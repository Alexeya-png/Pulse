from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import requests

from .model import Member, Sample, Snapshot, username

IG_WEB_APP_ID = "936619743392459"
USERNAME_RE = re.compile(r"^[a-z0-9_.]{1,30}$")
RELATION_BASES = (
    "https://i.instagram.com/api/v1/friendships",
    "https://www.instagram.com/api/v1/friendships",
)
PROFILE_URLS = (
    "https://i.instagram.com/api/v1/users/{user_id}/info/",
    "https://www.instagram.com/api/v1/users/{user_id}/info/",
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


def _normalize_session_data(session_data: object) -> tuple[dict[str, str], str]:
    if not isinstance(session_data, dict):
        raise InstagramDirectError("Нужно один раз войти в Instagram в настройках Pulse.")
    raw_cookies = session_data.get("cookies")
    cookies: dict[str, str] = {}
    if isinstance(raw_cookies, dict):
        for key, value in raw_cookies.items():
            if value is not None:
                cookies[str(key)] = str(value)
    else:
        for key in ("sessionid", "csrftoken", "ds_user_id", "mid", "ig_did", "rur"):
            value = session_data.get(key)
            if value is not None:
                cookies[key] = str(value)
    if not cookies.get("sessionid"):
        raise InstagramDirectError("Вход в Instagram не завершён. Откройте настройки и войдите ещё раз.")
    user_agent = str(session_data.get("user_agent") or DEFAULT_UA).strip() or DEFAULT_UA
    return cookies, user_agent


def _make_session(session_data: object) -> tuple[requests.Session, dict[str, str]]:
    cookies, user_agent = _normalize_session_data(session_data)
    session = requests.Session()
    session.headers.update({
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/",
        "User-Agent": user_agent,
        "X-IG-App-ID": IG_WEB_APP_ID,
        "X-Requested-With": "XMLHttpRequest",
    })
    csrf = cookies.get("csrftoken")
    if csrf:
        session.headers["X-CSRFToken"] = csrf
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=".instagram.com", path="/")
    return session, cookies


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

        final_url = response.url.lower()
        if "/challenge/" in final_url or "/checkpoint/" in final_url:
            raise InstagramDirectError("Instagram просит подтвердить вход. Откройте настройки и войдите ещё раз.")
        if "/accounts/login" in final_url:
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
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _count(user: dict, direct: str, edge: str) -> int | None:
    value = _as_int(user.get(direct))
    if value is not None:
        return value
    nested = user.get(edge)
    return _as_int(nested.get("count")) if isinstance(nested, dict) else None


def _extract_user(data: dict | None) -> dict | None:
    if not isinstance(data, dict):
        return None
    user = data.get("user")
    if isinstance(user, dict):
        return user
    nested = data.get("data")
    if isinstance(nested, dict):
        user = nested.get("user")
        if isinstance(user, dict):
            return user
    return data if data.get("username") else None


def _session_user_id(cookies: dict[str, str]) -> str | None:
    value = str(cookies.get("ds_user_id") or "").strip()
    if value.isdigit():
        return value
    sessionid = str(cookies.get("sessionid") or "")
    prefix = sessionid.split("%3A", 1)[0].split(":", 1)[0].strip()
    return prefix if prefix.isdigit() else None


def _current_profile(
    session: requests.Session,
    cookies: dict[str, str],
    target: str,
    deadline: float,
) -> dict:
    target = username(target)
    user_id = _session_user_id(cookies)
    candidates: list[tuple[str, dict | None]] = []
    if user_id:
        candidates.extend((url.format(user_id=user_id), None) for url in PROFILE_URLS)
    candidates.extend([
        ("https://i.instagram.com/api/v1/accounts/current_user/", {"edit": "true"}),
        ("https://www.instagram.com/api/v1/accounts/current_user/", {"edit": "true"}),
        ("https://www.instagram.com/api/v1/users/web_profile_info/", {"username": target}),
        ("https://i.instagram.com/api/v1/users/web_profile_info/", {"username": target}),
    ])

    saw_auth_error = False
    for url, params in candidates:
        status, data = _json_get(session, url, params, deadline)
        if status in (401, 403):
            saw_auth_error = True
            continue
        if status == 429:
            continue
        if status != 200:
            continue
        user = _extract_user(data)
        if not user:
            continue
        returned_username = str(user.get("username") or "").strip().lower()
        returned_id = user.get("pk") or user.get("id") or user.get("pk_id") or user_id
        if not returned_username or returned_id is None:
            continue
        if returned_username != target:
            raise InstagramDirectError(
                f"В Pulse выполнен вход как @{returned_username}. Для точного списка войдите как @{target}."
            )
        if user_id and str(returned_id) != user_id:
            raise InstagramDirectError(
                f"Сессия Instagram принадлежит другому аккаунту. Войдите в Pulse как @{target}."
            )
        followers_count = _count(user, "follower_count", "edge_followed_by")
        following_count = _count(user, "following_count", "edge_follow")
        if followers_count is None or following_count is None:
            continue
        return {
            "id": str(returned_id),
            "username": returned_username,
            "followers_count": followers_count,
            "following_count": following_count,
            "is_private": bool(user.get("is_private")),
        }

    if saw_auth_error:
        raise InstagramDirectError("Сессия Instagram истекла. Откройте настройки Pulse и войдите ещё раз.")
    raise InstagramDirectError("Instagram не вернул данные вашего профиля. Попробуйте войти ещё раз.")


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
    if status in (401, 403):
        raise InstagramDirectError("Сессия Instagram истекла. Откройте настройки Pulse и войдите ещё раз.")
    if status in (404, 429):
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
                raise InstagramDirectError("Список изменился прямо во время проверки. Запустите сбор ещё раз.")
            if len(found) == expected:
                return tuple(found.values())
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            time.sleep(min(REQUEST_DELAY, _deadline_left(deadline)))

    label = "подписок" if kind == "following" else "подписчиков"
    raise InstagramDirectError(
        f"Instagram вернул неполный список {label}: {len(found)} из {expected}. Снимок не сохранён."
    )


def collect_snapshot_direct(
    account: str,
    session_data: object,
    timeout_seconds: int = 180,
) -> Snapshot:
    target = username(account)
    deadline = time.monotonic() + max(30, min(int(timeout_seconds), 210))
    session, cookies = _make_session(session_data)
    try:
        before = _current_profile(session, cookies, target, deadline)
        user_id = before["id"]
        following = _collect_relation(
            session,
            user_id,
            "following",
            before["following_count"],
            deadline,
        )
        followers = _collect_relation(
            session,
            user_id,
            "followers",
            before["followers_count"],
            deadline,
        )
        after = _current_profile(session, cookies, target, deadline)
        if after["id"] != user_id:
            raise InstagramDirectError("Аккаунт изменился во время проверки. Запустите сбор ещё раз.")
        if (
            after["followers_count"] != before["followers_count"]
            or after["following_count"] != before["following_count"]
        ):
            raise InstagramDirectError("Списки изменились прямо во время проверки. Запустите сбор ещё раз.")
        if len(followers) != before["followers_count"] or len(following) != before["following_count"]:
            raise InstagramDirectError("Получены неполные списки. Снимок не сохранён.")
        return Snapshot(
            target,
            datetime.now(timezone.utc).isoformat(),
            (
                Sample("followers", "", followers, "id"),
                Sample("following", "", following, "id"),
            ),
            "instagram-device-direct",
        )
    finally:
        session.close()
