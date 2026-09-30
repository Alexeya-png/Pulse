from __future__ import annotations

from .instagram_direct import InstagramDirectError, collect_snapshot_direct
from .model import DataError, Snapshot, username


class CheckerError(DataError):
    pass


def configured() -> bool:
    return True


def collect_snapshot(account: str, session_data: object | None = None) -> Snapshot:
    account = username(account)
    if not session_data:
        raise CheckerError("Нужно один раз войти в Instagram в настройках Pulse.")
    try:
        return collect_snapshot_direct(account, session_data)
    except InstagramDirectError as exc:
        raise CheckerError(str(exc)) from None
