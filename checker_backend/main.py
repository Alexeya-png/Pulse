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
    # Validate the session before allocating a browser.
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


def _open_relation(page, target: str, kind: str):
    href = f"/{target}/{kind}/"
    locator = page.locator(f'a[href="{href}"]').first
    if locator.count() == 0:
        raise HTTPException(
            403,
            "Instagram не показывает этот список checker-аккаунту.",
        )
    locator.click()
    dialog = page.locator('div[role="dialog"]').last
    dialog.wait_for(state="visible", timeout=15000)
    return dialog


def _extract_usernames(dialog) -> dict[str, dict]:
    rows = dialog.evaluate(
        r"""dialog => {
            const out = [];
            for (const a of dialog.querySelectorAll('a[href]')) {
                let path;
                try { path = new URL(a.href).pathname; } catch (_) { continue; }
                const m = path.match(/^\/([A-Za-z0-9._]{1,30})\/$/);
                if (!m) continue;
                const u = m[1].toLowerCase();
                if (['accounts','explore','reels','direct'].includes(u)) continue;
                out.push(u);
            }
            return [...new Set(out)];
        }"""
    )
    return {u: {"id": u, "username": u} for u in rows if USERNAME_RE.fullmatch(u)}


def _scroll_relation(dialog, expected: int | None, label: str) -> list[dict]:
    found: dict[str, dict] = {}
    stable_rounds = 0
    last_count = -1

    for _ in range(1200):
        found.update(_extract_usernames(dialog))
        if len(found) > MAX_MEMBERS:
            raise HTTPException(413, f"{label}: список больше лимита сервера.")

        if expected is not None and len(found) >= expected:
            break

        state = dialog.evaluate(
            """dialog => {
                const all = [dialog, ...dialog.querySelectorAll('*')];
                const candidates = all.filter(
                    e => e.scrollHeight > e.clientHeight + 20
                );
                candidates.sort((a,b) => (b.scrollHeight-b.clientHeight) - (a.scrollHeight-a.clientHeight));
                const sc = candidates[0];
                if (!sc) return {moved:false, end:true};
                const before = sc.scrollTop;
                sc.scrollTop = Math.min(
                    sc.scrollHeight,
                    sc.scrollTop + Math.max(500, sc.clientHeight * 0.85)
                );
                const end = sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 8;
                return {moved: sc.scrollTop !== before, end};
            }"""
        )

        if len(found) == last_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
            last_count = len(found)

        if state.get("end") and stable_rounds >= 8:
            break
        if stable_rounds >= 18:
            break
        time.sleep(0.35)

    found.update(_extract_usernames(dialog))
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

        expected_followers = _exact_count(page, target, "followers")
        expected_following = _exact_count(page, target, "following")

        followers_dialog = _open_relation(page, target, "followers")
        followers = _scroll_relation(
            followers_dialog, expected_followers, "Подписчики"
        )
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)

        following_dialog = _open_relation(page, target, "following")
        following = _scroll_relation(
            following_dialog, expected_following, "Подписки"
        )
        page.keyboard.press("Escape")

        if expected_followers is None or expected_following is None:
            raise HTTPException(
                409,
                "Не удалось подтвердить точное количество списков. Снимок не сохранён.",
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
