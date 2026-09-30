from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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
            "User-Agent": "Pulse-Android/0.5",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=180) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read(1024 * 1024).decode("utf-8"))
            message = payload.get("detail")
        except Exception:
            message = None
        raise CheckerError(message or "Instagram не завершил проверку.") from None
    except (URLError, TimeoutError, OSError):
        raise CheckerError("Не удалось связаться с сервером проверки.") from None

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
