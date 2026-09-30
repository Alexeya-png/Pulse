from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

import requests
from fastapi import HTTPException

USERNAME_RE = re.compile(r"^[a-z0-9_.]{1,30}$")
MAX_MEMBERS = int(os.environ.get("MAX_MEMBERS", "500000"))
IG_WEB_APP_ID = "936619743392459"
IG_BROWSER_UA = os.environ.get(
    "IG_DIRECT_BROWSER_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
)
IG_APP_UA = os.environ.get(
    "IG_DIRECT_APP_UA",
    "Instagram 389.0.0.49.87 Android (34/14; 420dpi; 1080x2400; Google/google; Pixel 7; panther; panther; en_US; 699134704)",
)
IG_REQUEST_DELAY = max(0.0, float(os.environ.get("IG_DIRECT_REQUEST_DELAY", "0.8")))
IG_429_RETRIES = max(0, int(os.environ.get("IG_DIRECT_429_RETRIES", "1")))
IG_429_BACKOFF = max(0.5, float(os.environ.get("IG_DIRECT_429_BACKOFF", "2.0")))
IG_MAX_RETRY_AFTER = max(1.0, float(os.environ.get("IG_DIRECT_MAX_RETRY_AFTER", "15")))
PROFILE_URLS = (
    "https://www.instagram.com/api/v1/users/web_profile_info/",
    "https://i.instagram.com/api/v1/users/web_profile_info/",
)
RELATION_BASES = (
    "https://i.instagram.com/api/v1/friendships",
    "https://www.instagram.com/api/v1/friendships",
)
RELATION_VARIANTS = (
    ("surface-100", {"count": 100, "search_surface": "follow_list_page"}),
)
_pace_lock = threading.Lock()
_last_request_at = 0.0
logger = logging.getLogger("uvicorn.error")


def _remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise HTTPException(504, "Время онлайн-сбора истекло. Снимок не сохранён.")
    return value


def _session_data(raw_session_json: str | None = None) -> dict[str, str]:
    raw = (
        str(raw_session_json).strip()
        if raw_session_json is not None
        else os.environ.get("IG_SESSION_JSON", "").strip()
    )
    if not raw:
        raise HTTPException(503, "Для прямого fallback нужен IG_SESSION_JSON checker-аккаунта.")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(503, "IG_SESSION_JSON имеет неверный JSON.") from None
    if not isinstance(payload, dict):
        raise HTTPException(503, "IG_SESSION_JSON имеет неверный формат.")

    data: dict[str, str] = {}
    nested = payload.get("cookies")
    if isinstance(nested, dict):
        for name, value in nested.items():
            if value is not None:
                data[str(name)] = str(value)
    elif isinstance(nested, list):
        for item in nested:
            if not isinstance(item, dict):
                continue
            name, value = item.get("name"), item.get("value")
            if name and value is not None:
                data[str(name)] = str(value)

    for name, value in payload.items():
        if name == "cookies" or isinstance(value, (dict, list)):
            continue
        if value is not None:
            data[str(name)] = str(value)

    if not data.get("sessionid"):
        raise HTTPException(503, "IG_SESSION_JSON не содержит sessionid checker-аккаунта.")
    return data


def _session_headers(user_agent: str) -> dict[str, str]:
    return {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": user_agent,
        "X-IG-App-ID": IG_WEB_APP_ID,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.instagram.com/",
    }


def _apply_cookies(
    session: requests.Session,
    data: dict[str, str],
    *,
    minimal: bool = False,
) -> None:
    allowed = {"sessionid", "csrftoken", "ds_user_id", "mid", "ig_did", "rur"}
    for name, value in data.items():
        if minimal and name not in allowed:
            continue
        session.cookies.set(name, value, domain=".instagram.com", path="/")
    csrf = data.get("csrftoken")
    if csrf:
        session.headers["X-CSRFToken"] = csrf


def _make_session(raw_session_json: str | None = None) -> requests.Session:
    data = _session_data(raw_session_json)
    session = requests.Session()
    session.headers.update(_session_headers(IG_BROWSER_UA))
    _apply_cookies(session, data)
    return session


def _fallback_sessions(source: requests.Session) -> list[tuple[str, requests.Session]]:
    cookies = requests.utils.dict_from_cookiejar(source.cookies)
    variants: list[tuple[str, requests.Session]] = []

    minimal = requests.Session()
    minimal.headers.update(_session_headers(IG_BROWSER_UA))
    _apply_cookies(minimal, cookies, minimal=True)
    variants.append(("minimal-browser", minimal))

    mobile = requests.Session()
    mobile.headers.update(_session_headers(IG_APP_UA))
    _apply_cookies(mobile, cookies, minimal=True)
    variants.append(("mobile-auth", mobile))

    anonymous = requests.Session()
    anonymous.headers.update(_session_headers(IG_BROWSER_UA))
    variants.append(("anonymous-browser", anonymous))

    return variants

def _pace(deadline: float) -> None:
    global _last_request_at
    if IG_REQUEST_DELAY <= 0:
        return
    with _pace_lock:
        remaining = _remaining(deadline)
        wait = IG_REQUEST_DELAY - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(min(wait, remaining))
        _remaining(deadline)
        _last_request_at = time.monotonic()


def _fetch_once(
    session: requests.Session,
    url: str,
    params: dict | None,
    deadline: float,
) -> tuple[int, dict | None]:
    _pace(deadline)
    remaining = _remaining(deadline)
    try:
        response = session.get(
            url,
            params=params,
            timeout=(min(8.0, remaining), min(30.0, remaining)),
            allow_redirects=True,
        )
    except requests.RequestException:
        raise HTTPException(502, "Instagram временно недоступен для прямого collector.") from None

    final_url = response.url.lower()
    if "/challenge/" in final_url or "/checkpoint/" in final_url:
        raise HTTPException(503, "Instagram просит подтвердить checker-аккаунт.")
    if "/accounts/login" in final_url:
        return 401, None

    try:
        data = response.json()
    except ValueError:
        data = None
    return response.status_code, data if isinstance(data, dict) else None


def _json_get(
    session: requests.Session,
    url: str,
    params: dict | None,
    deadline: float,
) -> tuple[int, dict | None]:
    status = 503
    data = None
    for attempt in range(IG_429_RETRIES + 1):
        status, data = _fetch_once(session, url, params, deadline)
        if status != 429:
            break
        if attempt >= IG_429_RETRIES:
            break
        delay = min(
            IG_429_BACKOFF * (2 ** attempt),
            IG_MAX_RETRY_AFTER,
            _remaining(deadline),
        )
        logger.warning("Direct Instagram transport rate-limited; retrying")
        time.sleep(delay)

    if status not in (401, 403, 429):
        return status, data

    best_status, best_data = status, data
    for name, alternative in _fallback_sessions(session):
        try:
            alt_status, alt_data = _fetch_once(alternative, url, params, deadline)
        finally:
            alternative.close()

        if alt_status not in (401, 403, 429):
            logger.info(
                "Direct Instagram transport recovered via %s (HTTP %d)",
                name,
                alt_status,
            )
            return alt_status, alt_data
        if best_status == 429 and alt_status != 429:
            best_status, best_data = alt_status, alt_data

    if best_status == 429:
        logger.warning("Direct Instagram endpoint rate-limited on all transports")
    return best_status, best_data

def _as_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _count(user: dict, direct: str, edge: str) -> int | None:
    value = _as_int(user.get(direct))
    if value is not None:
        return value
    nested = user.get(edge)
    if isinstance(nested, dict):
        return _as_int(nested.get("count"))
    return None


def _profile_from_user(user: dict, target: str | None = None) -> dict | None:
    if not isinstance(user, dict):
        return None
    username = str(user.get("username") or "").strip().lower()
    if target and username and username != target:
        return None
    user_id = user.get("pk") or user.get("id") or user.get("pk_id")
    if user_id is None:
        return None
    return {
        "id": str(user_id),
        "username": username or (target or ""),
        "followers_count": _count(user, "follower_count", "edge_followed_by"),
        "following_count": _count(user, "following_count", "edge_follow"),
        "is_private": bool(user.get("is_private")),
    }


def _extract_profile(data: dict | None, target: str) -> dict | None:
    if not isinstance(data, dict):
        return None
    candidates: list[dict] = []
    nested = data.get("data")
    if isinstance(nested, dict):
        user = nested.get("user")
        if isinstance(user, dict):
            candidates.append(user)
        candidates.append(nested)
    user = data.get("user")
    if isinstance(user, dict):
        candidates.append(user)
    candidates.append(data)
    for candidate in candidates:
        profile = _profile_from_user(candidate, target)
        if profile:
            return profile
    return None


def _user_info(session: requests.Session, user_id: str, target: str, deadline: float) -> dict | None:
    status, data = _json_get(
        session,
        f"https://i.instagram.com/api/v1/users/{user_id}/info/",
        None,
        deadline,
    )
    if status in (401, 403) or status != 200 or not data:
        return None
    user = data.get("user") if isinstance(data.get("user"), dict) else data
    return _profile_from_user(user, target)


def _search_profile(session: requests.Session, target: str, deadline: float) -> dict | None:
    status, data = _json_get(
        session,
        "https://www.instagram.com/api/v1/web/search/topsearch/",
        {"context": "blended", "query": target, "include_reel": "false"},
        deadline,
    )
    if status != 200 or not data:
        return None
    for item in data.get("users") or []:
        if not isinstance(item, dict):
            continue
        user = item.get("user") if isinstance(item.get("user"), dict) else item
        profile = _profile_from_user(user, target)
        if profile:
            return profile
    return None


def _parse_exact_count(value: str) -> int | None:
    compact = value.replace(",", "").replace(" ", "").strip()
    if not compact.isdigit():
        return None
    return int(compact)


def _profile_page_counts(
    session: requests.Session,
    target: str,
    deadline: float,
) -> tuple[int | None, int | None]:
    url = f"https://www.instagram.com/{target}/"
    sessions: list[tuple[str, requests.Session]] = [("primary", session)]
    sessions.extend(_fallback_sessions(session))
    try:
        for name, candidate in sessions:
            _pace(deadline)
            remaining = _remaining(deadline)
            try:
                response = candidate.get(
                    url,
                    timeout=(min(8.0, remaining), min(30.0, remaining)),
                    allow_redirects=True,
                )
            except requests.RequestException:
                continue
            if response.status_code != 200:
                continue

            text = response.text
            patterns = (
                (
                    r'"edge_followed_by"\s*:\s*\{\s*"count"\s*:\s*(\d+)',
                    r'"edge_follow"\s*:\s*\{\s*"count"\s*:\s*(\d+)',
                ),
                (
                    r'"follower_count"\s*:\s*(\d+)',
                    r'"following_count"\s*:\s*(\d+)',
                ),
                (
                    r'([\d, ]+)\s+Followers\s*,\s*([\d, ]+)\s+Following',
                    None,
                ),
            )
            for followers_pattern, following_pattern in patterns:
                first = re.search(followers_pattern, text, re.IGNORECASE)
                if not first:
                    continue
                if following_pattern is None:
                    followers = _parse_exact_count(first.group(1))
                    following = _parse_exact_count(first.group(2))
                else:
                    second = re.search(following_pattern, text, re.IGNORECASE)
                    if not second:
                        continue
                    followers = _parse_exact_count(first.group(1))
                    following = _parse_exact_count(second.group(1))
                if followers is not None and following is not None:
                    logger.info("Direct exact profile counts recovered from profile page via %s", name)
                    return followers, following
    finally:
        for name, candidate in sessions:
            if name != "primary":
                candidate.close()
    return None, None


def _profile(session: requests.Session, target: str, deadline: float) -> dict:
    best = None
    saw_forbidden = False
    saw_not_found = False
    saw_rate_limit = False

    for url in PROFILE_URLS:
        status, data = _json_get(session, url, {"username": target}, deadline)
        if status == 404:
            saw_not_found = True
            continue
        if status == 429:
            saw_rate_limit = True
            continue
        if status in (401, 403):
            saw_forbidden = True
            continue
        if status != 200:
            continue
        profile = _extract_profile(data, target)
        if not profile:
            continue
        best = profile
        if profile["followers_count"] is not None and profile["following_count"] is not None:
            return profile

    # Search can still work when web_profile_info is cloud-rate-limited.
    search = _search_profile(session, target, deadline)
    if search:
        if not best:
            best = search
        info = _user_info(session, best["id"], target, deadline)
        if info:
            best = info

    # The public profile HTML often exposes exact small-account counts in
    # embedded JSON or OG metadata even when API endpoints return 429.
    followers_count, following_count = _profile_page_counts(session, target, deadline)
    if best and followers_count is not None and following_count is not None:
        best["followers_count"] = followers_count
        best["following_count"] = following_count

    if best and best["followers_count"] is not None and best["following_count"] is not None:
        return best
    if best:
        raise HTTPException(503, "Instagram не вернул точные счётчики профиля.")
    if saw_forbidden:
        raise HTTPException(503, "Instagram не принял checker-сессию с Render.")
    if saw_not_found and not saw_rate_limit:
        raise HTTPException(404, "Instagram-аккаунт не найден.")
    if saw_rate_limit:
        raise HTTPException(503, "Instagram ограничил профильные endpoints с сервера.")
    raise HTTPException(503, "Instagram не дал получить профиль прямому collector.")

def _member(row: dict) -> dict:
    user_id = row.get("pk") or row.get("id") or row.get("pk_id")
    username = str(row.get("username") or "").strip().lower()
    if user_id is None or not USERNAME_RE.fullmatch(username):
        raise HTTPException(503, "Instagram вернул неполные данные пользователя.")
    return {"id": str(user_id), "username": username}


def _relationship_sessions(
    source: requests.Session,
) -> list[tuple[str, requests.Session, bool]]:
    cookies = requests.utils.dict_from_cookiejar(source.cookies)
    variants: list[tuple[str, requests.Session, bool]] = [
        ("primary-browser", source, False),
    ]

    minimal = requests.Session()
    minimal.headers.update(_session_headers(IG_BROWSER_UA))
    _apply_cookies(minimal, cookies, minimal=True)
    variants.append(("minimal-browser", minimal, True))

    mobile = requests.Session()
    mobile.headers.update(_session_headers(IG_APP_UA))
    _apply_cookies(mobile, cookies, minimal=True)
    variants.append(("mobile-auth", mobile, True))
    return variants


def _relation_page_from_base(
    session: requests.Session,
    base: str,
    user_id: str,
    kind: str,
    max_id: str | None,
    deadline: float,
    base_params: dict | None = None,
) -> tuple[list, str | None] | None:
    params = dict(base_params or {"count": 100, "search_surface": "follow_list_page"})
    if max_id:
        params["max_id"] = max_id

    status, data = _json_get(
        session,
        f"{base}/{user_id}/{kind}/",
        params,
        deadline,
    )
    if status in (401, 403, 404, 429):
        return None
    if status != 200 or not data or data.get("status") == "fail":
        return None

    users = data.get("users")
    if not isinstance(users, list):
        return None
    cursor = data.get("next_max_id")
    if cursor in (None, ""):
        cursor = data.get("next_cursor")
    return users, str(cursor) if cursor not in (None, "") else None


def _collect_pages(
    session: requests.Session,
    user_id: str,
    kind: str,
    expected: int,
    label: str,
    deadline: float,
    *,
    allow_partial: bool = False,
) -> list[dict]:
    if expected > MAX_MEMBERS:
        raise HTTPException(413, f"{label}: список больше лимита сервера.")
    if expected == 0:
        return []

    # Instagram can return HTTP 200 with a relationship list that is shorter
    # than the public profile counter. Different authenticated transports,
    # hosts and pagination parameters can expose different slices. Merge all
    # observed rows by immutable numeric ID and only accept the exact count.
    found: dict[str, dict] = {}
    cookie_map = requests.utils.dict_from_cookiejar(session.cookies)
    has_authenticated_session = bool(cookie_map.get("sessionid"))
    transport_sessions = (
        _relationship_sessions(session)
        if has_authenticated_session
        else [("primary-browser", session, False)]
    )
    relation_variants = (
        RELATION_VARIANTS
        if has_authenticated_session
        else RELATION_VARIANTS[:1]
    )
    try:
        for transport_name, candidate, _close_candidate in transport_sessions:
            for variant_name, variant_params in relation_variants:
                for base in RELATION_BASES:
                    max_id = None
                    seen_cursors: set[str] = set()
                    host_rows = 0

                    while True:
                        _remaining(deadline)
                        page = _relation_page_from_base(
                            candidate,
                            base,
                            user_id,
                            kind,
                            max_id,
                            deadline,
                            variant_params,
                        )
                        if page is None:
                            break
                        users, cursor = page
                        host_rows += len(users)
                        for row in users:
                            member = _member(row)
                            found[member["id"]] = member

                        if len(found) > expected:
                            raise HTTPException(
                                409,
                                f"{label}: список изменился во время проверки.",
                            )
                        if len(found) == expected:
                            logger.info(
                                "Direct %s complete after transport/variant merge "
                                "(%s, %s, %d/%d)",
                                kind,
                                transport_name,
                                variant_name,
                                len(found),
                                expected,
                            )
                            return list(found.values())

                        if not cursor:
                            break
                        if cursor in seen_cursors:
                            logger.warning(
                                "Direct %s pagination loop (%s, %s)",
                                kind,
                                transport_name,
                                variant_name,
                            )
                            break
                        seen_cursors.add(cursor)
                        max_id = cursor

                    logger.info(
                        "Direct %s slice %s/%s/%s produced %d raw rows; combined %d/%d",
                        kind,
                        transport_name,
                        variant_name,
                        base.split("//", 1)[-1].split("/", 1)[0],
                        host_rows,
                        len(found),
                        expected,
                    )
    finally:
        for _name, candidate, close_candidate in transport_sessions:
            if close_candidate:
                candidate.close()

    if len(found) != expected:
        logger.warning(
            "Direct %s relationship incomplete after all transports/variants: %d/%d",
            kind,
            len(found),
            expected,
        )
        if allow_partial:
            return list(found.values())
        raise HTTPException(
            409,
            f"{label}: получено {len(found)} из {expected}. Снимок не сохранён.",
        )
    return list(found.values())


def collect_direct_snapshot(
    target: str,
    deadline: float,
    session_json: str | None = None,
) -> dict:
    with _make_session(session_json) as session:
        before = _profile(session, target, deadline)
        if before["is_private"]:
            raise HTTPException(403, "Онлайн-режим поддерживает только публичные Instagram-аккаунты.")
        user_id = before["id"]
        followers_count = before["followers_count"]
        following_count = before["following_count"]
        logger.info(
            "Direct Instagram exact counts resolved (%d followers, %d following)",
            followers_count,
            following_count,
        )

        following = _collect_pages(
            session,
            user_id,
            "following",
            following_count,
            "Подписки",
            deadline,
            allow_partial=True,
        )
        following_complete = len(following) == following_count

        # Followers drive follow/unfollow history and remain strict. Never
        # accept a partial follower list because that could create false events.
        followers = _collect_pages(
            session, user_id, "followers", followers_count, "Подписчики", deadline
        )
        followers_complete = len(followers) == followers_count

        after = _profile(session, target, deadline)
        if after["id"] != user_id:
            raise HTTPException(409, "Аккаунт изменился во время проверки.")
        if (
            after["followers_count"] != followers_count
            or after["following_count"] != following_count
        ):
            raise HTTPException(409, "Списки изменились прямо во время проверки.")
        if not followers_complete:
            raise HTTPException(409, "Получен неполный список подписчиков. Снимок не сохранён.")

        logger.info(
            "Direct Instagram snapshot collected (%d/%d followers, %d/%d following)",
            len(followers),
            followers_count,
            len(following),
            following_count,
        )
        return {
            "account": target,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "followers_count": followers_count,
            "following_count": following_count,
            "followers": followers,
            "following": following,
            "followers_complete": followers_complete,
            "following_complete": following_complete,
            "complete": followers_complete and following_complete,
            "source": (
                "pulse-direct-server"
                if following_complete
                else "pulse-direct-server-following-partial"
            ),
        }
