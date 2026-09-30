from __future__ import annotations

import json
import socket
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import certifi

from .checker_config import BACKEND_BASE_URL
from .model import DataError, Member, Sample, Snapshot, username

MAX_RESPONSE_BYTES = 96 * 1024 * 1024


class CheckerError(DataError):
    pass


def configured() -> bool:
    return (
        isinstance(BACKEND_BASE_URL, str)
        and BACKEND_BASE_URL.startswith("https://")
        and "YOUR-PULSE-CHECKER" not in BACKEND_BASE_URL
    )


def _ssl_context():
    return ssl.create_default_context(cafile=certifi.where())


def _request(request: Request, timeout: int):
    last_error = None
    for attempt in range(3):
        try:
            return urlopen(request, timeout=timeout, context=_ssl_context())
        except HTTPError:
            raise
        except (URLError, TimeoutError, OSError, ssl.SSLError, socket.timeout) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    reason = getattr(last_error, "reason", last_error)
    text = str(reason or "").lower()
    if "certificate" in text or "ssl" in text:
        raise CheckerError("Ошибка защищённого соединения с сервером. Обновите Pulse.") from None
    if "timed out" in text or "timeout" in text:
        raise CheckerError("Сервер проверки долго отвечает. Попробуйте ещё раз через минуту.") from None
    raise CheckerError("Не удалось связаться с сервером проверки.") from None


def collect_snapshot(account: str) -> Snapshot:
    account = username(account)
    if not configured():
        raise CheckerError("Сервер проверки ещё не настроен.")

    body = json.dumps({"username": account}).encode("utf-8")
    request = Request(
        BACKEND_BASE_URL.rstrip("/") + "/v1/collect",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Pulse-Android/0.5.1",
        },
        method="POST",
    )

    try:
        response = _request(request, 210)
        with response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read(1024 * 1024).decode("utf-8"))
            message = payload.get("detail")
        except Exception:
            message = None
        raise CheckerError(message or f"Сервер вернул ошибку {exc.code}.") from None

    if len(raw) > MAX_RESPONSE_BYTES:
        raise CheckerError("Ответ слишком большой для обработки на устройстве.")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise CheckerError("Сервер вернул повреждённый ответ.") from None

    if payload.get("account") != account:
        raise CheckerError("Сервер вернул данные другого аккаунта.")

    def members(key: str) -> tuple[Member, ...]:
        rows = payload.get(key)
        if not isinstance(rows, list):
            raise CheckerError("Сервер вернул неполный список.")
        result = []
        for row in rows:
            if not isinstance(row, dict):
                raise CheckerError("Сервер вернул некорректный список.")
            value = row.get("username")
            user_id = row.get("id")
            if user_id is not None:
                user_id = str(user_id)
            result.append(Member(value, user_id))
        return tuple(result)

    followers = members("followers")
    following = members("following")
    return Snapshot(
        account,
        payload.get("captured_at"),
        (
            Sample("followers", "", followers, "id"),
            Sample("following", "", following, "id"),
        ),
        "checker_account",
    )
