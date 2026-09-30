from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import requests

from .model import Member, Sample, Snapshot, username

IG_WEB_APP_ID = "936619743392459"
USERNAME_RE = re.compile(r"^[a-z0-9_.]{1,30}$")
PROFILE_URLS = (
    "https://www.instagram.com/api/v1/users/web_profile_info/",
    "https://i.instagram.com/api/v1/users/web_profile_info/",
)
RELATION_BASES = (
    "https://i.instagram.com/api/v1/friendships",
    "https://www.instagram.com/api/v1/friendships",
)
DEFAULT_UA = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36"
)
MAX_MEMBERS = 500000
REQUEST_TIMEOUT = 25
REQUEST_DELAY = 0.35


class InstagramDirectError(ValueError):
    pass


def _deadline_left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise InstagramDirectError("Instagram слишком долго отвечает. Снимок не сохранён.")
    return left


def _make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/",
        "User-Agent": DEFAULT_UA,
        "X-IG-App-ID": IG_WEB_APP_ID,
        "X-Requested-With": "XMLHttpRequest",
    })
    return session


def _json_get(
    session: requests.Session,
    url: str,
    params: dict | None,
    deadline: float,
) -> tuple[int, dict | None]:
    last_status = 503
    for attempt in range(3):
        remaining = _deadline_left(deadline)
        try:
            response = session.get(
                url,
                params=params,
                timeout=(min(7, remaining), min(REQUEST_TIMEOUT, remaining)),
                allow_redirects=True,
            )
        except requests.RequestException:
            raise InstagramDirectError("Не удалось связаться с Instagram. Проверьте интернет.") from None

        if "/accounts/login" in response.url.lower():
            return 401, None

        last_status = response.status_code
        try:
            data = response.json()
        except ValueError:
            data = None

        if response.status_code != 429:
            return response.status_code, data if isinstance(data, dict) else None
        if attempt < 2:
            retry_after = response.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else 1.5 * (attempt + 1)
            except ValueError:
                wait = 1.5 * (attempt + 1)
            time.sleep(min(max(wait, 0.5), 6.0, _deadline_left(deadline)))
    return last_status, None


def _as_int(value) -> int | None:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _count(user: dict, direct: str, edge: str) -> int | None:
    value = _as_int(user.get(direct))
    if value is not None:
        return value
    nested = user.get(edge)
    return _as_int(nested.get("count")) if isinstance(nested, dict) else None


def _extract_profile(data: dict | None, target: str) -> dict | None:
    if not isinstance(data, dict):
        return None
    candidates: list[dict] = []
    nested = data.get("data")
    if isinstance(nested, dict):
        user = nested.get("user")
        if isinstance(user, dict):
            candidates.append(user)
    user = data.get("user")
    if isinstance(user, dict):
        candidates.append(user)
    candidates.append(data)

    for candidate in candidates:
        returned_username = str(candidate.get("username") or "").strip().lower()
        user_id = candidate.get("pk") or candidate.get("id") or candidate.get("pk_id")
        if returned_username != target or user_id is None:
            continue
        followers_count = _count(candidate, "follower_count", "edge_followed_by")
        following_count = _count(candidate, "following_count", "edge_follow")
        if followers_count is None or following_count is None:
            continue
        return {
            "id": str(user_id),
            "username": returned_username,
            "followers_count": followers_count,
            "following_count": following_count,
            "is_private": bool(candidate.get("is_private")),
        }
    return None


def _search_profile(session: requests.Session, target: str, deadline: float) -> dict | None:
    status, data = _json_get(
        session,
        "https://www.instagram.com/api/v1/web/search/topsearch/",
        {"context": "blended", "query": target, "include_reel": "false"},
        deadline,
    )
    if status != 200 or not isinstance(data, dict):
        return None
    for item in data.get("users") or []:
        if not isinstance(item, dict):
            continue
        user = item.get("user") if isinstance(item.get("user"), dict) else item
        profile = _extract_profile({"user": user}, target)
        if profile:
            return profile
    return None


def _profile(session: requests.Session, target: str, deadline: float) -> dict:
    for url in PROFILE_URLS:
        status, data = _json_get(session, url, {"username": target}, deadline)
        if status == 200:
            profile = _extract_profile(data, target)
            if profile:
                return profile

    profile = _search_profile(session, target, deadline)
    if profile:
        return profile

    raise InstagramDirectError(
        "Instagram не отдал публичные данные профиля без авторизации."
    )


def _member(row: dict) -> Member:
    user_id = row.get("pk") or row.get("id") or row.get("pk_id")
    member_username = str(row.get("username") or "").strip().lower()
    if user_id is None or not str(user_id).isdigit() or not USERNAME_RE.fullmatch(member_username):
        raise InstagramDirectError("Instagram вернул неполные данные одного из пользователей.")
    return Member(member_username, str(user_id))


def _relation_page(
    session: requests.Session,
    base: str,
    user_id: str,
    kind: str,
    cursor: str | None,
    deadline: float,
) -> tuple[list[dict], str | None] | None:
    params = {"count": 200, "search_surface": "follow_list_page"}
    if cursor:
        params["max_id"] = cursor
    status, data = _json_get(session, f"{base}/{user_id}/{kind}/", params, deadline)

    # No login is performed. Auth-gated relationship endpoints simply mean that
    # this anonymous transport is unavailable and the caller can use our backend.
    if status in (401, 403, 404, 429):
        return None
    if status != 200 or not isinstance(data, dict) or data.get("status") == "fail":
        return None
    users = data.get("users")
    if not isinstance(users, list):
        return None
    next_cursor = data.get("next_max_id")
    if next_cursor is None:
        next_cursor = data.get("next_cursor")
    return users, str(next_cursor) if next_cursor not in (None, "") else None


def _collect_relation(
    session: requests.Session,
    user_id: str,
    kind: str,
    expected: int,
    deadline: float,
) -> tuple[Member, ...]:
    if expected > MAX_MEMBERS:
        raise InstagramDirectError("Список слишком большой для этой версии Pulse.")
    if expected == 0:
        return ()

    found: dict[str, Member] = {}
    for base in RELATION_BASES:
        cursor = None
        seen_cursors: set[str] = set()
        while True:
            page = _relation_page(session, base, user_id, kind, cursor, deadline)
            if page is None:
                break
            rows, next_cursor = page
            for row in rows:
                member = _member(row)
                found[member.user_id or member.username] = member
            if len(found) > expected:
                raise InstagramDirectError("Список изменился прямо во время проверки.")
            if len(found) == expected:
                return tuple(found.values())
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            time.sleep(min(REQUEST_DELAY, _deadline_left(deadline)))

    label = "подписок" if kind == "following" else "подписчиков"
    raise InstagramDirectError(
        f"Instagram без входа отдал неполный список {label}: {len(found)} из {expected}."
    )


def collect_snapshot_direct(
    account: str,
    timeout_seconds: int = 90,
) -> Snapshot:
    target = username(account)
    deadline = time.monotonic() + max(20, min(int(timeout_seconds), 120))
    session = _make_session()
    try:
        before = _profile(session, target, deadline)
        if before["is_private"]:
            raise InstagramDirectError("Приватный Instagram-аккаунт нельзя проверить без авторизации.")

        user_id = before["id"]
        following = _collect_relation(
            session, user_id, "following", before["following_count"], deadline
        )
        followers = _collect_relation(
            session, user_id, "followers", before["followers_count"], deadline
        )
        after = _profile(session, target, deadline)

        if after["id"] != user_id:
            raise InstagramDirectError("Аккаунт изменился во время проверки.")
        if (
            after["followers_count"] != before["followers_count"]
            or after["following_count"] != before["following_count"]
        ):
            raise InstagramDirectError("Списки изменились прямо во время проверки.")
        if len(followers) != before["followers_count"] or len(following) != before["following_count"]:
            raise InstagramDirectError("Получены неполные списки. Снимок не сохранён.")

        return Snapshot(
            target,
            datetime.now(timezone.utc).isoformat(),
            (
                Sample("followers", "", followers, "id"),
                Sample("following", "", following, "id"),
            ),
            "instagram-device-anonymous",
        )
    finally:
        session.close()
