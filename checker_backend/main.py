from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from checker_backend.browser_runtime import browser_session, smoke_check


@asynccontextmanager
async def lifespan(app):
    await asyncio.to_thread(smoke_check)
    yield


app = FastAPI(title="Pulse Checker", version="0.4.0", lifespan=lifespan)

USERNAME_RE = re.compile(r"^[a-z0-9_.]{1,30}$")
MAX_MEMBERS = int(os.environ.get("MAX_MEMBERS", "500000"))
IG_WEB_APP_ID = "936619743392459"
_collect_lock = threading.Lock()
logger = logging.getLogger("uvicorn.error")


class CollectRequest(BaseModel):
    username: str


def _normal_username(value: str) -> str:
    value = (value or "").strip().removeprefix("@").lower()
    if not USERNAME_RE.fullmatch(value):
        raise HTTPException(400, "Некорректный Instagram-ник.")
    return value


def _session_cookies() -> list[dict]:
    raw = os.environ.get("IG_SESSION_JSON", "").strip()
    if not raw:
        raise HTTPException(
            503,
            "Нашему collector нужен IG_SESSION_JSON checker-аккаунта.",
        )
    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(503, "IG_SESSION_JSON имеет неверный JSON.") from None

    if not isinstance(data, dict) or not data.get("sessionid"):
        raise HTTPException(
            503,
            "IG_SESSION_JSON не содержит sessionid checker-аккаунта.",
        )

    cookies = []
    for name, value in data.items():
        if value is None:
            continue
        cookies.append(
            {
                "name": str(name),
                "value": str(value),
                "domain": ".instagram.com",
                "path": "/",
                "secure": True,
                "httpOnly": name == "sessionid",
                "sameSite": "Lax",
            }
        )
    return cookies


@contextmanager
def _make_page():
    cookies = _session_cookies()
    with browser_session() as browser:
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="UTC",
        )
        try:
            context.add_cookies(cookies)
            page = context.new_page()
            page.set_default_timeout(15000)
            yield context, page
        finally:
            context.close()


def _assert_logged_in(page) -> None:
    page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1500)
    url = page.url.lower()
    if "/challenge/" in url or "/checkpoint/" in url:
        raise HTTPException(
            503,
            "Instagram просит подтвердить checker-аккаунт. Откройте его в обычном Instagram и подтвердите вход.",
        )
    if "/accounts/login" in url:
        raise HTTPException(
            503,
            "IG_SESSION_JSON checker-аккаунта истёк. Создайте новую Chrome-сессию.",
        )


def _exact_count(page, target: str, kind: str) -> int | None:
    href = f"/{target}/{kind}/"
    value = page.evaluate(
        """href => {
            const a = [...document.querySelectorAll('a[href]')]
                .find(x => {
                    try { return new URL(x.href).pathname.toLowerCase() === href.toLowerCase(); }
                    catch (_) { return false; }
                });
            if (!a) return null;
            const titled = a.querySelector('[title]')?.getAttribute('title');
            return titled || a.getAttribute('aria-label') || a.textContent || null;
        }""",
        href,
    )
    if not value:
        return None
    text = str(value).strip().lower()
    if re.search(r"\b[\d.,]+\s*[km]\b", text):
        return None
    match = re.search(r"(\d[\d\s,.]*)", text)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


def _ig_json(page, url: str) -> tuple[int, dict | None]:
    result = page.evaluate(
        """async ({url, appId}) => {
            const response = await fetch(url, {
                method: 'GET',
                credentials: 'include',
                headers: {
                    'X-IG-App-ID': appId,
                    'X-Requested-With': 'XMLHttpRequest',
                    'Accept': '*/*'
                }
            });
            let data = null;
            try { data = await response.json(); } catch (_) {}
            return {status: response.status, data};
        }""",
        {"url": url, "appId": IG_WEB_APP_ID},
    )
    if not isinstance(result, dict):
        raise HTTPException(503, "Instagram вернул неожиданный ответ.")
    status = int(result.get("status") or 0)
    data = result.get("data")
    return status, data if isinstance(data, dict) else None


def _count_from_profile(user: dict, kind: str) -> int | None:
    if kind == "followers":
        candidates = (
            user.get("follower_count"),
            (user.get("edge_followed_by") or {}).get("count")
            if isinstance(user.get("edge_followed_by"), dict) else None,
        )
    else:
        candidates = (
            user.get("following_count"),
            (user.get("edge_follow") or {}).get("count")
            if isinstance(user.get("edge_follow"), dict) else None,
        )
    for value in candidates:
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _resolve_profile(page, target: str) -> dict:
    profile_url = (
        "https://www.instagram.com/api/v1/users/web_profile_info/"
        f"?username={quote(target, safe='')}"
    )
    status, data = _ig_json(page, profile_url)
    if status == 404:
        raise HTTPException(404, "Instagram-аккаунт не найден.")

    if status == 200 and data:
        user = data.get("data", {}).get("user")
        if isinstance(user, dict):
            username = str(user.get("username") or "").lower()
            user_id = user.get("id") or user.get("pk")
            if username == target and user_id is not None:
                return {
                    "id": str(user_id),
                    "followers_count": _count_from_profile(user, "followers"),
                    "following_count": _count_from_profile(user, "following"),
                }

    search_url = (
        "https://www.instagram.com/api/v1/web/search/topsearch/"
        f"?context=blended&query={quote(target, safe='')}&include_reel=false"
    )
    search_status, search_data = _ig_json(page, search_url)
    if search_status in (401, 403):
        raise HTTPException(
            503,
            "Instagram не принял checker-сессию для чтения данных. Обновите IG_SESSION_JSON.",
        )
    if search_status == 429:
        raise HTTPException(
            503,
            "Instagram временно ограничил запросы checker-аккаунта. Попробуйте позже.",
        )
    if search_status != 200 or not search_data:
        raise HTTPException(
            503,
            f"Instagram не дал определить аккаунт для сбора (HTTP {search_status or 'unknown'}).",
        )

    for item in search_data.get("users") or []:
        user = item.get("user") if isinstance(item, dict) else None
        if not isinstance(user, dict):
            continue
        if str(user.get("username") or "").lower() != target:
            continue
        user_id = user.get("pk") or user.get("id")
        if user_id is not None:
            return {
                "id": str(user_id),
                "followers_count": _count_from_profile(user, "followers"),
                "following_count": _count_from_profile(user, "following"),
            }

    raise HTTPException(404, "Instagram-аккаунт не найден.")


def _relation_page(page, user_id: str, kind: str, max_id: str = "") -> tuple[list, str]:
    url = (
        f"https://www.instagram.com/api/v1/friendships/{quote(user_id, safe='')}/{kind}/"
        "?count=50"
    )
    if max_id:
        url += f"&max_id={quote(max_id, safe='')}"

    status, data = _ig_json(page, url)
    if status in (401,):
        raise HTTPException(
            503,
            "IG_SESSION_JSON checker-аккаунта истёк или Instagram отклонил сессию.",
        )
    if status == 403:
        raise HTTPException(
            403,
            "Instagram не разрешил checker-аккаунту читать этот список.",
        )
    if status == 429:
        raise HTTPException(
            503,
            "Instagram временно ограничил запросы checker-аккаунта. Попробуйте позже.",
        )
    if status != 200 or not data:
        raise HTTPException(
            503,
            f"Instagram не вернул список {kind} (HTTP {status or 'unknown'}).",
        )
    if data.get("status") == "fail":
        raise HTTPException(503, f"Instagram отклонил получение списка {kind}.")

    users = data.get("users")
    if not isinstance(users, list):
        raise HTTPException(503, f"Instagram вернул неверный формат списка {kind}.")
    return users, str(data.get("next_max_id") or "")


def _collect_relation(page, user_id: str, kind: str, expected: int | None, label: str) -> list[dict]:
    if expected == 0:
        return []

    found: dict[str, dict] = {}
    next_max_id = ""
    seen_cursors: set[str] = set()

    for _ in range(10000):
        users, cursor = _relation_page(page, user_id, kind, next_max_id)
        for user in users:
            if not isinstance(user, dict):
                continue
            username = str(user.get("username") or "").strip().lower()
            if USERNAME_RE.fullmatch(username):
                found[username] = {"id": username, "username": username}

        if len(found) > MAX_MEMBERS:
            raise HTTPException(413, f"{label}: список больше лимита сервера.")
        if expected is not None and len(found) >= expected:
            break
        if not cursor:
            break
        if cursor in seen_cursors:
            break
        seen_cursors.add(cursor)
        next_max_id = cursor
        time.sleep(0.35)

    return list(found.values())


def _collect_profile(target: str) -> dict:
    with _make_page() as (context, page):
        _assert_logged_in(page)

        page.goto(
            f"https://www.instagram.com/{target}/",
            wait_until="domcontentloaded",
            timeout=45000,
        )
        page.wait_for_timeout(1800)

        body = page.locator("body").inner_text(timeout=10000).lower()
        if "sorry, this page isn't available" in body:
            raise HTTPException(404, "Instagram-аккаунт не найден.")

        profile = _resolve_profile(page, target)
        expected_followers = profile.get("followers_count")
        expected_following = profile.get("following_count")

        if expected_followers is None:
            expected_followers = _exact_count(page, target, "followers")
        if expected_following is None:
            expected_following = _exact_count(page, target, "following")

        if expected_followers is None or expected_following is None:
            raise HTTPException(
                409,
                "Не удалось подтвердить точное количество списков. Снимок не сохранён.",
            )

        followers = _collect_relation(
            page, profile["id"], "followers", expected_followers, "Подписчики"
        )
        following = _collect_relation(
            page, profile["id"], "following", expected_following, "Подписки"
        )

        if len(followers) != expected_followers:
            raise HTTPException(
                409,
                f"Подписчики: получено {len(followers)} из {expected_followers}. Снимок не сохранён.",
            )
        if len(following) != expected_following:
            raise HTTPException(
                409,
                f"Подписки: получено {len(following)} из {expected_following}. Снимок не сохранён.",
            )

        return {
            "account": target,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "followers_count": expected_followers,
            "following_count": expected_following,
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
