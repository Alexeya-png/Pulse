from __future__ import annotations

import json

from .client import COOKIE_NAMES, DEFAULT_USER_AGENT, make_client
from .errors import SyncError
from .instagram import InstagramReader
from .model import DataError, Snapshot, username


class CheckerError(DataError):
    pass


def session_settings_from_json(raw: str) -> dict:
    if not isinstance(raw, str) or not raw.strip():
        raise CheckerError("Вставьте Checker session JSON.")
    try:
        data = json.loads(raw)
    except ValueError:
        raise CheckerError("Checker session JSON имеет неверный формат.") from None
    if not isinstance(data, dict):
        raise CheckerError("Checker session JSON должен быть объектом.")

    source = data.get("cookies") if isinstance(data.get("cookies"), dict) else data
    cookies = {}
    for name in COOKIE_NAMES:
        value = source.get(name)
        if value is None:
            continue
        value = str(value)
        if value:
            cookies[name] = value

    if not cookies.get("sessionid"):
        raise CheckerError("В Checker session JSON нет sessionid.")

    agent = data.get("user_agent") or DEFAULT_USER_AGENT
    account = data.get("account")
    settings = {
        "version": 3,
        "cookies": cookies,
        "user_agent": str(agent),
    }
    if account:
        settings["account"] = str(account).strip().lstrip("@").lower()
    return settings


def collect_snapshot(
    account: str,
    settings: dict,
    *,
    cancel=None,
    progress=None,
) -> Snapshot:
    account = username(account)
    client = make_client(cancel=cancel, delay=1.5)
    try:
        client.set_settings(settings)
        result = InstagramReader(
            client,
            cancel=cancel,
            progress=progress,
            request_limit=250,
        ).collect(account)
        return result.snapshot
    except SyncError as exc:
        raise CheckerError(str(exc)) from None
    finally:
        client.close()
