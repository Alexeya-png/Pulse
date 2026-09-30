from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from instagrapi import Client
from pydantic import BaseModel

app = FastAPI(title="Pulse Checker", version="0.2.0")
logger = logging.getLogger("pulse.checker")

USERNAME_RE = re.compile(r"^[a-z0-9_.]{1,30}$")
MAX_MEMBERS = int(os.environ.get("MAX_MEMBERS", "500000"))
SESSION_FILE = Path("/tmp/pulse-instagram-session.json")

_collect_lock = threading.Lock()
_client_lock = threading.Lock()
_client = None
_client_username = None


class CollectRequest(BaseModel):
    username: str


def _normal_username(value: str) -> str:
    value = (value or "").strip().removeprefix("@").lower()
    if not USERNAME_RE.fullmatch(value):
        raise HTTPException(400, "Некорректный Instagram-ник.")
    return value


def _load_env_session(client: Client) -> bool:
    raw = os.environ.get("IG_SESSION_JSON", "").strip()
    if not raw:
        return False
    try:
        settings = json.loads(raw)
        if not isinstance(settings, dict):
            raise ValueError
        client.set_settings(settings)
        return True
    except Exception:
        raise HTTPException(503, "IG_SESSION_JSON имеет неверный формат.") from None


def _make_client() -> Client:
    username = os.environ.get("IG_CHECKER_USERNAME", "").strip().lower()
    password = os.environ.get("IG_CHECKER_PASSWORD", "").strip()
    if not username:
        raise HTTPException(503, "Проверяющий Instagram-аккаунт ещё не настроен.")
    if not password and not os.environ.get("IG_SESSION_JSON", "").strip():
        raise HTTPException(
            503,
            "Для проверяющего аккаунта нужен IG_CHECKER_PASSWORD или IG_SESSION_JSON.",
        )

    client = Client()
    loaded_session = False

    try:
        if SESSION_FILE.exists():
            client.load_settings(SESSION_FILE)
            loaded_session = True
        elif _load_env_session(client):
            loaded_session = True

        if password:
            client.login(username, password)
        else:
            # A saved session can be reused without exposing the password to the app.
            if not loaded_session:
                raise HTTPException(503, "Сессия Instagram не настроена.")
            sessionid = (client.get_settings().get("cookies") or {}).get("sessionid")
            if not sessionid:
                raise HTTPException(503, "IG_SESSION_JSON не содержит рабочую Instagram-сессию.")
            client.login_by_sessionid(sessionid)

        try:
            client.dump_settings(SESSION_FILE)
        except Exception:
            pass

        return client
    except HTTPException:
        raise
    except Exception as exc:
        name = exc.__class__.__name__
        message = str(exc or "")
        safe = message.replace(password, "***") if password else message
        logger.warning("Instagram checker login failed: %s: %s", name, safe[:500])

        lower = (name + " " + message).lower()
        if "twofactor" in lower or "2fa" in lower:
            raise HTTPException(
                503,
                "Instagram требует 2FA. Нужна заранее сохранённая IG_SESSION_JSON.",
            ) from None
        if "challenge" in lower or "checkpoint" in lower:
            raise HTTPException(
                503,
                "Instagram запросил подтверждение входа. Подтвердите checker-аккаунт в официальном Instagram и повторите.",
            ) from None
        if "please wait" in lower or "rate" in lower or "429" in lower:
            raise HTTPException(
                429,
                "Instagram временно ограничил checker-аккаунт. Не повторяйте вход несколько минут.",
            ) from None
        if "badpassword" in lower or "bad password" in lower:
            raise HTTPException(503, "Неверный пароль checker-аккаунта.") from None
        raise HTTPException(
            503,
            "Instagram не разрешил войти checker-аккаунту.",
        ) from None


def _get_client() -> Client:
    global _client, _client_username
    configured_username = os.environ.get("IG_CHECKER_USERNAME", "").strip().lower()
    with _client_lock:
        if _client is None or _client_username != configured_username:
            _client = _make_client()
            _client_username = configured_username
        return _client


def _invalidate_client() -> None:
    global _client, _client_username
    with _client_lock:
        _client = None
        _client_username = None
    try:
        SESSION_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _member(row) -> dict:
    user_id = str(getattr(row, "pk"))
    value = str(getattr(row, "username")).lower()
    return {"id": user_id, "username": value}


def _collect_list(iterator, expected: int | None, label: str) -> list[dict]:
    result = {}
    try:
        for row in iterator:
            member = _member(row)
            result[member["id"]] = member
            if len(result) > MAX_MEMBERS:
                raise HTTPException(413, f"{label}: список больше лимита сервера.")
    except HTTPException:
        raise
    except Exception as exc:
        name = exc.__class__.__name__.lower()
        message = str(exc or "").lower()
        if "loginrequired" in name or "login_required" in message:
            _invalidate_client()
            raise HTTPException(503, "Сессия checker-аккаунта истекла.") from None
        if "ratelimit" in name or "please wait" in message or "429" in message:
            raise HTTPException(429, "Instagram временно ограничил проверку. Попробуйте позже.") from None
        raise HTTPException(502, f"{label}: Instagram прервал получение списка. Снимок не сохранён.") from None

    if expected is not None and len(result) != expected:
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
        "engine": "instagrapi-mobile",
    }


@app.post("/v1/collect")
def collect(request: CollectRequest):
    target = _normal_username(request.username)

    if not _collect_lock.acquire(blocking=False):
        raise HTTPException(429, "Сейчас уже выполняется другая проверка. Попробуйте позже.")

    try:
        client = _get_client()

        try:
            info = client.user_info_by_username(target)
        except Exception as exc:
            name = exc.__class__.__name__.lower()
            message = str(exc or "").lower()
            if "usernotfound" in name or "not found" in message:
                raise HTTPException(404, "Instagram-аккаунт не найден.") from None
            if "loginrequired" in name or "login_required" in message:
                _invalidate_client()
                raise HTTPException(503, "Сессия checker-аккаунта истекла.") from None
            raise HTTPException(502, "Instagram не отдал профиль для проверки.") from None

        user_id = str(info.pk)
        expected_followers = getattr(info, "follower_count", None)
        expected_following = getattr(info, "following_count", None)
        if expected_followers is not None:
            expected_followers = int(expected_followers)
        if expected_following is not None:
            expected_following = int(expected_following)

        if expected_followers is not None and expected_followers > MAX_MEMBERS:
            raise HTTPException(413, "Подписчики: список больше лимита сервера.")
        if expected_following is not None and expected_following > MAX_MEMBERS:
            raise HTTPException(413, "Подписки: список больше лимита сервера.")

        followers = _collect_list(
            client.iter_user_followers_v1(user_id, amount=0, page_size=200),
            expected_followers,
            "Подписчики",
        )
        following = _collect_list(
            client.iter_user_following_v1(user_id, amount=0, page_size=200),
            expected_following,
            "Подписки",
        )

        try:
            after = client.user_info(user_id)
        except Exception:
            raise HTTPException(
                502,
                "Не удалось подтвердить полноту снимка. Снимок не сохранён.",
            ) from None

        after_followers = getattr(after, "follower_count", None)
        after_following = getattr(after, "following_count", None)

        if (
            expected_followers is not None
            and after_followers is not None
            and int(after_followers) != expected_followers
        ) or (
            expected_following is not None
            and after_following is not None
            and int(after_following) != expected_following
        ):
            raise HTTPException(
                409,
                "Списки изменились прямо во время проверки. Запустите сбор ещё раз.",
            )

        return {
            "account": target,
            "account_id": user_id,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "followers_count": expected_followers,
            "following_count": expected_following,
            "followers": followers,
            "following": following,
            "complete": True,
        }
    finally:
        _collect_lock.release()
