from __future__ import annotations

import requests

from .checker_config import BACKEND_BASE_URL
from .instagram_direct import InstagramDirectError, collect_snapshot_direct
from .model import DataError, Member, Sample, Snapshot, username


class CheckerError(DataError):
    pass


def configured() -> bool:
    return bool(BACKEND_BASE_URL)


def _backend_snapshot(account: str) -> Snapshot:
    try:
        response = requests.post(
            BACKEND_BASE_URL.rstrip("/") + "/v1/collect",
            json={"username": account},
            timeout=(10, 220),
        )
    except requests.RequestException:
        raise CheckerError(
            "Не удалось связаться с нашим collector. Проверьте интернет и попробуйте ещё раз."
        ) from None

    try:
        payload = response.json()
    except ValueError:
        payload = None

    if response.status_code != 200:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if isinstance(detail, str) and detail.strip():
            raise CheckerError(detail.strip())
        raise CheckerError("Наш collector не смог получить полный список. Снимок не сохранён.")

    if not isinstance(payload, dict) or payload.get("complete") is not True:
        raise CheckerError("Collector вернул неполный результат. Снимок не сохранён.")

    followers_raw = payload.get("followers")
    following_raw = payload.get("following")
    if not isinstance(followers_raw, list) or not isinstance(following_raw, list):
        raise CheckerError("Collector вернул данные в неверном формате.")

    try:
        followers = tuple(
            Member(str(row["username"]), str(row["id"]))
            for row in followers_raw
            if isinstance(row, dict)
        )
        following = tuple(
            Member(str(row["username"]), str(row["id"]))
            for row in following_raw
            if isinstance(row, dict)
        )
        followers_count = int(payload.get("followers_count"))
        following_count = int(payload.get("following_count"))
    except (KeyError, TypeError, ValueError, DataError):
        raise CheckerError("Collector вернул данные в неверном формате.") from None

    if len(followers) != len(followers_raw) or len(following) != len(following_raw):
        raise CheckerError("Collector вернул неполные записи пользователей.")
    if len(followers) != followers_count or len(following) != following_count:
        raise CheckerError("Количество пользователей не совпало со счётчиками. Снимок не сохранён.")

    captured_at = payload.get("captured_at")
    if not isinstance(captured_at, str):
        raise CheckerError("Collector не вернул время снимка.")

    source = str(payload.get("source") or "pulse-direct-server")
    return Snapshot(
        account,
        captured_at,
        (
            Sample("followers", "", followers, "id"),
            Sample("following", "", following, "id"),
        ),
        source,
    )


def collect_snapshot(account: str, session_data: object | None = None) -> Snapshot:
    account = username(account)

    # First try a completely anonymous request from the phone's own network.
    # No Instagram login, cookies, password, or user session are used.
    try:
        return collect_snapshot_direct(account)
    except InstagramDirectError:
        pass

    # If Instagram does not expose the public relationship list anonymously,
    # fall back to our own server-side collector. The user still never signs in.
    return _backend_snapshot(account)
