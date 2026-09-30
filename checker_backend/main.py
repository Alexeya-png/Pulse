from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone

import instaloader
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Pulse Checker", version="0.1.1")
logger = logging.getLogger("pulse.checker")

USERNAME_RE = re.compile(r"^[a-z0-9_.]{1,30}$")
MAX_MEMBERS = int(os.environ.get("MAX_MEMBERS", "500000"))

_collect_lock = threading.Lock()
_loader_lock = threading.Lock()
_loader = None
_loader_username = None


class CollectRequest(BaseModel):
    username: str


def _normal_username(value: str) -> str:
    value = (value or "").strip().removeprefix("@").lower()
    if not USERNAME_RE.fullmatch(value):
        raise HTTPException(400, "Некорректный Instagram-ник.")
    return value


def _make_loader() -> instaloader.Instaloader:
    username = os.environ.get("IG_CHECKER_USERNAME", "").strip().lower()
    if not username:
        raise HTTPException(503, "Проверяющий Instagram-аккаунт ещё не настроен.")

    loader = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
        max_connection_attempts=1,
        request_timeout=30.0,
    )

    session_json = os.environ.get("IG_SESSION_JSON", "").strip()
    password = os.environ.get("IG_CHECKER_PASSWORD", "").strip()

    try:
        if session_json:
            session = json.loads(session_json)
            if not isinstance(session, dict):
                raise ValueError
            loader.load_session(username, session)
        elif password:
            loader.login(username, password)
        else:
            raise HTTPException(
                503,
                "Для проверяющего аккаунта нужен IG_SESSION_JSON или IG_CHECKER_PASSWORD.",
            )

        logged_in_as = loader.test_login()
        if not logged_in_as or logged_in_as.lower() != username:
            raise HTTPException(503, "Instagram-сессия проверяющего аккаунта недействительна.")
    except HTTPException:
        raise
    except instaloader.exceptions.TwoFactorAuthRequiredException:
        raise HTTPException(
            503,
            "Instagram запросил 2FA. Создайте сессию один раз и задайте IG_SESSION_JSON.",
        ) from None
    except instaloader.exceptions.BadCredentialsException:
        raise HTTPException(503, "Неверные данные проверяющего Instagram-аккаунта.") from None
    except instaloader.exceptions.InstaloaderException as exc:
        message = str(exc or "")
        safe_message = message.replace(password, "***") if password else message
        logger.warning(
            "Instagram checker login failed: %s: %s",
            exc.__class__.__name__,
            safe_message[:500],
        )
        lower = message.lower()
        if "checkpoint" in lower or "challenge" in lower:
            raise HTTPException(
                503,
                "Instagram запросил подтверждение входа/checkpoint. Откройте checker-аккаунт в Instagram, подтвердите вход и затем повторите проверку. Если повторяется — используйте IG_SESSION_JSON.",
            ) from None
        if "two-factor" in lower or "2fa" in lower:
            raise HTTPException(
                503,
                "Instagram требует 2FA. Для этого аккаунта нужен IG_SESSION_JSON.",
            ) from None
        if "login" in lower and ("required" in lower or "please wait" in lower):
            raise HTTPException(
                503,
                "Instagram временно не принимает серверный вход. Подтвердите вход в самом Instagram и попробуйте снова.",
            ) from None
        raise HTTPException(
            503,
            "Instagram отклонил серверный вход. Для стабильной работы нужен IG_SESSION_JSON, созданный после обычного входа в аккаунт.",
        ) from None
    except (ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(503, "IG_SESSION_JSON имеет неверный формат.") from None

    return loader


def _get_loader() -> instaloader.Instaloader:
    global _loader, _loader_username
    configured_username = os.environ.get("IG_CHECKER_USERNAME", "").strip().lower()
    with _loader_lock:
        if _loader is None or _loader_username != configured_username:
            _loader = _make_loader()
            _loader_username = configured_username
        return _loader


def _invalidate_loader() -> None:
    global _loader, _loader_username
    with _loader_lock:
        _loader = None
        _loader_username = None


def _collect_members(iterator, expected: int, label: str) -> list[dict]:
    if expected > MAX_MEMBERS:
        raise HTTPException(413, f"{label}: список больше лимита сервера.")

    result = {}
    try:
        for profile in iterator:
            user_id = str(profile.userid)
            result[user_id] = {
                "id": user_id,
                "username": profile.username.lower(),
            }
            if len(result) > MAX_MEMBERS:
                raise HTTPException(413, f"{label}: список больше лимита сервера.")
    except HTTPException:
        raise
    except instaloader.exceptions.LoginRequiredException:
        _invalidate_loader()
        raise HTTPException(503, "Сессия проверяющего аккаунта истекла.") from None
    except instaloader.exceptions.TooManyRequestsException:
        raise HTTPException(429, "Instagram временно ограничил проверки. Попробуйте позже.") from None
    except instaloader.exceptions.ConnectionException:
        raise HTTPException(502, "Instagram прервал получение списка. Снимок не сохранён.") from None
    except instaloader.exceptions.InstaloaderException:
        raise HTTPException(502, "Не удалось полностью получить список Instagram.") from None

    if len(result) != expected:
        raise HTTPException(
            409,
            f"{label}: Instagram отдал не весь список ({len(result)} из {expected}). Снимок не сохранён.",
        )
    return list(result.values())


@app.get("/health")
def health():
    return {
        "ok": True,
        "checker_configured": bool(os.environ.get("IG_CHECKER_USERNAME")),
        "has_password": bool(os.environ.get("IG_CHECKER_PASSWORD")),
        "has_session": bool(os.environ.get("IG_SESSION_JSON")),
    }


@app.post("/v1/collect")
def collect(request: CollectRequest):
    target = _normal_username(request.username)

    if not _collect_lock.acquire(blocking=False):
        raise HTTPException(429, "Сейчас уже выполняется другая проверка. Попробуйте позже.")

    try:
        loader = _get_loader()
        try:
            before = instaloader.Profile.from_username(loader.context, target)
        except instaloader.exceptions.ProfileNotExistsException:
            raise HTTPException(404, "Instagram-аккаунт не найден.") from None
        except instaloader.exceptions.LoginRequiredException:
            _invalidate_loader()
            raise HTTPException(503, "Сессия проверяющего аккаунта истекла.") from None
        except instaloader.exceptions.ConnectionException:
            raise HTTPException(502, "Instagram временно недоступен.") from None

        if before.is_private and not before.followed_by_viewer:
            raise HTTPException(
                403,
                "Аккаунт приватный. Проверяющий аккаунт должен быть его подписчиком.",
            )

        before_followers = int(before.followers)
        before_following = int(before.followees)

        followers = _collect_members(
            before.get_followers(), before_followers, "Подписчики"
        )
        following = _collect_members(
            before.get_followees(), before_following, "Подписки"
        )

        try:
            after = instaloader.Profile.from_username(loader.context, target)
        except instaloader.exceptions.InstaloaderException:
            raise HTTPException(
                502, "Не удалось подтвердить полноту снимка. Снимок не сохранён."
            ) from None

        if (
            str(after.userid) != str(before.userid)
            or int(after.followers) != before_followers
            or int(after.followees) != before_following
        ):
            raise HTTPException(
                409,
                "Списки изменились прямо во время проверки. Запустите сбор ещё раз.",
            )

        return {
            "account": target,
            "account_id": str(before.userid),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "followers_count": before_followers,
            "following_count": before_following,
            "followers": followers,
            "following": following,
            "complete": True,
        }
    finally:
        _collect_lock.release()
