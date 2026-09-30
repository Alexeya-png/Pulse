from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Pulse Checker", version="0.4.1")

USERNAME_RE = re.compile(r"^[a-z0-9_.]{1,30}$")
MAX_MEMBERS = int(os.environ.get("MAX_MEMBERS", "500000"))
IG_WEB_APP_ID = "936619743392459"
IG_REQUEST_DELAY = max(0.0, float(os.environ.get("IG_REQUEST_DELAY", "1.0")))
IG_429_RETRIES = max(0, int(os.environ.get("IG_429_RETRIES", "4")))
IG_429_BACKOFF = max(0.5, float(os.environ.get("IG_429_BACKOFF", "4")))
IG_MAX_RETRY_AFTER = max(1.0, float(os.environ.get("IG_MAX_RETRY_AFTER", "120")))
PROFILE_URLS = (
    "https://www.instagram.com/api/v1/users/web_profile_info/",
    "https://i.instagram.com/api/v1/users/web_profile_info/",
)
RELATION_BASES = (
    "https://i.instagram.com/api/v1/friendships",
    "https://www.instagram.com/api/v1/friendships",
)
_collect_lock = threading.Lock()
_request_pace_lock = threading.Lock()
_last_request_at = 0.0
logger = logging.getLogger("uvicorn.error")


class CollectRequest(BaseModel):
    username: str


def _normal_username(value: str) -> str:
    value = (value or "").strip().removeprefix("@").lower()
    if not USERNAME_RE.fullmatch(value):
        raise HTTPException(400, "Некорректный Instagram-ник.")
    return value


def _session_data() -> dict[str, str]:
    raw = os.environ.get("IG_SESSION_JSON", "").strip()
    if not raw:
        raise HTTPException(503, "Нашему collector нужен IG_SESSION_JSON checker-аккаунта.")
    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(503, "IG_SESSION_JSON имеет неверный JSON.") from None
    if not isinstance(data, dict) or not data.get("sessionid"):
        raise HTTPException(503, "IG_SESSION_JSON не содержит sessionid checker-аккаунта.")
    return {str(k): str(v) for k, v in data.items() if v is not None}


def _make_session() -> requests.Session:
    data = _session_data()
    session = requests.Session()
    session.headers.update({
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": "Instagram 123.0.0.21.114",
        "X-IG-App-ID": IG_WEB_APP_ID,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.instagram.com/",
    })
    for name, value in data.items():
        session.cookies.set(name, value, domain=".instagram.com", path="/")
    return session


def _pace_request() -> None:
    global _last_request_at
    if IG_REQUEST_DELAY <= 0:
        return
    with _request_pace_lock:
        now = time.monotonic()
        wait = IG_REQUEST_DELAY - (now - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _retry_after_seconds(response: requests.Response, attempt: int) -> float:
    raw = str(response.headers.get("Retry-After") or "").strip()
    if raw:
        if raw.isdigit():
            return min(float(raw), IG_MAX_RETRY_AFTER)
        try:
            retry_at = parsedate_to_datetime(raw)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
            if seconds > 0:
                return min(seconds, IG_MAX_RETRY_AFTER)
        except (TypeError, ValueError, OverflowError):
            pass
    return min(IG_429_BACKOFF * (2 ** attempt), IG_MAX_RETRY_AFTER)


def _json_get(
    session: requests.Session,
    url: str,
    params: dict | None = None,
) -> tuple[int, dict | None]:
    response = None
    for attempt in range(IG_429_RETRIES + 1):
        _pace_request()
        try:
            response = session.get(
                url,
                params=params,
                timeout=(10, 45),
                allow_redirects=True,
            )
        except requests.RequestException:
            raise HTTPException(
                502,
                "Instagram временно недоступен для нашего collector.",
            ) from None

        final_url = response.url.lower()
        if "/challenge/" in final_url or "/checkpoint/" in final_url:
            raise HTTPException(
                503,
                "Instagram просит подтвердить checker-аккаунт.",
            )
        if "/accounts/login" in final_url:
            raise HTTPException(
                503,
                "IG_SESSION_JSON checker-аккаунта истёк. Создайте новую Chrome-сессию.",
            )

        if response.status_code != 429:
            break

        if attempt >= IG_429_RETRIES:
            logger.warning(
                "Instagram 429 persisted after %d retries",
                IG_429_RETRIES,
            )
            raise HTTPException(
                503,
                "Instagram продолжает ограничивать запросы после автоматических повторов. Подождите и запустите проверку позже.",
            )

        delay = _retry_after_seconds(response, attempt)
        logger.warning(
            "Instagram 429; retrying in %.1fs (%d/%d)",
            delay,
            attempt + 1,
            IG_429_RETRIES,
        )
        time.sleep(delay)

    if response is None:
        raise HTTPException(502, "Instagram не вернул ответ.")

    try:
        data = response.json()
    except ValueError:
        data = None
    return response.status_code, data if isinstance(data, dict) else None


def _as_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _count(user: dict, direct: str, edge: str) -> int | None:
    value = _as_int(user.get(direct))
    if value is not None:
        return value
    edge_value = user.get(edge)
    if isinstance(edge_value, dict):
        return _as_int(edge_value.get("count"))
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
    candidates = []
    nested_data = data.get("data")
    if isinstance(nested_data, dict):
        if isinstance(nested_data.get("user"), dict):
            candidates.append(nested_data["user"])
        candidates.append(nested_data)
    if isinstance(data.get("user"), dict):
        candidates.append(data["user"])
    candidates.append(data)
    for candidate in candidates:
        profile = _profile_from_user(candidate, target)
        if profile:
            return profile
    return None


def _user_info(
    session: requests.Session,
    user_id: str,
    target: str,
) -> dict | None:
    status, data = _json_get(
        session,
        f"https://i.instagram.com/api/v1/users/{user_id}/info/",
    )
    if status in (401, 403):
        return None
    if status != 200 or not data:
        return None
    user = data.get("user") if isinstance(data.get("user"), dict) else data
    return _profile_from_user(user, target)


def _search_profile(
    session: requests.Session,
    target: str,
) -> dict | None:
    status, data = _json_get(
        session,
        "https://www.instagram.com/api/v1/web/search/topsearch/",
        {
            "context": "blended",
            "query": target,
            "include_reel": "false",
        },
    )
    if status in (401, 403):
        return None
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


def _profile(session: requests.Session, target: str) -> dict:
    best: dict | None = None
    saw_forbidden = False
    saw_not_found = False

    for url in PROFILE_URLS:
        status, data = _json_get(session, url, {"username": target})
        if status == 404:
            saw_not_found = True
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
        if (
            profile["followers_count"] is not None
            and profile["following_count"] is not None
        ):
            return profile

    if best:
        info = _user_info(session, best["id"], target)
        if info:
            if (
                info["followers_count"] is not None
                and info["following_count"] is not None
            ):
                return info
            best = info

    if not best:
        best = _search_profile(session, target)
        if best:
            info = _user_info(session, best["id"], target)
            if info:
                best = info

    if (
        best
        and best["followers_count"] is not None
        and best["following_count"] is not None
    ):
        return best
    if best:
        raise HTTPException(
            503,
            "Instagram не вернул точные счётчики профиля. Снимок не сохранён.",
        )
    if saw_forbidden:
        raise HTTPException(
            503,
            "Instagram не принял checker-сессию для чтения профиля. Обновите IG_SESSION_JSON.",
        )
    if saw_not_found:
        raise HTTPException(404, "Instagram-аккаунт не найден.")
    raise HTTPException(503, "Instagram не дал получить профиль для сбора.")


def _member(row: dict) -> dict:
    if not isinstance(row, dict):
        raise HTTPException(
            503,
            "Instagram вернул некорректный элемент списка.",
        )
    user_id = row.get("pk") or row.get("id") or row.get("pk_id")
    username = str(row.get("username") or "").strip().lower()
    if user_id is None or not USERNAME_RE.fullmatch(username):
        raise HTTPException(
            503,
            "Instagram вернул неполные данные пользователя.",
        )
    return {"id": str(user_id), "username": username}


def _relation_page(
    session: requests.Session,
    user_id: str,
    kind: str,
    max_id: str | None,
) -> tuple[list, str | None]:
    params = {"count": 50}
    if max_id:
        params["max_id"] = max_id

    saw_forbidden = False
    for base in RELATION_BASES:
        status, data = _json_get(
            session,
            f"{base}/{user_id}/{kind}/",
            params,
        )
        if status == 401:
            raise HTTPException(
                503,
                "IG_SESSION_JSON checker-аккаунта истёк или Instagram отклонил сессию.",
            )
        if status == 403:
            saw_forbidden = True
            continue
        if status != 200 or not data:
            continue
        if data.get("status") == "fail":
            continue
        users = data.get("users")
        if not isinstance(users, list):
            raise HTTPException(
                503,
                f"{kind}: Instagram вернул неполную страницу.",
            )
        cursor = data.get("next_max_id")
        return users, str(cursor) if cursor else None

    if saw_forbidden:
        raise HTTPException(
            403,
            "Instagram не разрешил checker-аккаунту читать этот список.",
        )
    raise HTTPException(503, f"Instagram не вернул список {kind}.")


def _collect_pages(
    session: requests.Session,
    user_id: str,
    kind: str,
    expected: int,
    label: str,
) -> list[dict]:
    if expected > MAX_MEMBERS:
        raise HTTPException(
            413,
            f"{label}: список больше лимита сервера.",
        )
    if expected == 0:
        return []

    result: dict[str, dict] = {}
    max_id: str | None = None
    seen_cursors: set[str] = set()

    while True:
        users, next_max_id = _relation_page(
            session,
            user_id,
            kind,
            max_id,
        )
        for row in users:
            member = _member(row)
            result[member["id"]] = member
            if len(result) > MAX_MEMBERS:
                raise HTTPException(
                    413,
                    f"{label}: список больше лимита сервера.",
                )

        if not next_max_id:
            break
        if next_max_id in seen_cursors:
            raise HTTPException(
                503,
                f"{label}: зациклилась пагинация. Снимок не сохранён.",
            )
        seen_cursors.add(next_max_id)
        max_id = next_max_id

    if len(result) != expected:
        raise HTTPException(
            409,
            f"{label}: получено {len(result)} из {expected}. Снимок не сохранён.",
        )
    return list(result.values())


def _collect_profile(target: str) -> dict:
    with _make_session() as session:
        before = _profile(session, target)
        user_id = before["id"]
        before_followers = before["followers_count"]
        before_following = before["following_count"]
        logger.info(
            "Instagram exact counts resolved (%d followers, %d following)",
            before_followers,
            before_following,
        )

        followers = _collect_pages(
            session,
            user_id,
            "followers",
            before_followers,
            "Подписчики",
        )
        following = _collect_pages(
            session,
            user_id,
            "following",
            before_following,
            "Подписки",
        )

        after = _profile(session, target)
        if after["id"] != user_id:
            raise HTTPException(
                409,
                "Аккаунт изменился во время проверки. Запустите сбор ещё раз.",
            )
        if (
            after["followers_count"] != before_followers
            or after["following_count"] != before_following
        ):
            raise HTTPException(
                409,
                "Списки изменились прямо во время проверки. Запустите сбор ещё раз.",
            )
        if len(followers) != after["followers_count"]:
            raise HTTPException(
                409,
                f"Подписчики: получено {len(followers)} из {after['followers_count']}. Снимок не сохранён.",
            )
        if len(following) != after["following_count"]:
            raise HTTPException(
                409,
                f"Подписки: получено {len(following)} из {after['following_count']}. Снимок не сохранён.",
            )

        logger.info(
            "Instagram complete snapshot collected (%d followers, %d following)",
            len(followers),
            len(following),
        )
        return {
            "account": target,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "followers_count": after["followers_count"],
            "following_count": after["following_count"],
            "followers": followers,
            "following": following,
            "complete": True,
            "source": "pulse-web-collector",
        }


@app.get("/health")
def health():
    return {
        "ok": True,
        "engine": "pulse-web-collector",
        "session_configured": bool(os.environ.get("IG_SESSION_JSON")),
        "hiker_dependency": False,
    }


@app.post("/v1/collect")
def collect(request: CollectRequest):
    target = _normal_username(request.username)
    if not _collect_lock.acquire(blocking=False):
        raise HTTPException(
            429,
            "Сейчас уже выполняется другая проверка. Попробуйте позже.",
        )
    try:
        try:
            return _collect_profile(target)
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("Collector failed (%s)", exc.__class__.__name__)
            raise HTTPException(
                503,
                f"Не удалось выполнить сбор нашим collector: {exc.__class__.__name__}.",
            ) from exc
    finally:
        _collect_lock.release()
