from __future__ import annotations

import requests

from .checker_config import BACKEND_BASE_URL
from .instagram_direct import InstagramDirectError, collect_snapshot_direct
from .model import DataError, Member, Sample, Snapshot, username


class CheckerError(DataError):
    pass


def configured() -> bool:
    return bool(BACKEND_BASE_URL)


def _parse_members(raw: object) -> tuple[Member, ...]:
    if not isinstance(raw, list):
        raise CheckerError("Collector вернул данные в неверном формате.")
    try:
        members = tuple(
            Member(str(row["username"]), str(row["id"]))
            for row in raw
            if isinstance(row, dict)
        )
    except (KeyError, TypeError, ValueError, DataError):
        raise CheckerError("Collector вернул данные в неверном формате.") from None
    if len(members) != len(raw):
        raise CheckerError("Collector вернул неполные записи пользователей.")
    return members


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise CheckerError("Collector вернул неверный счётчик пользователей.")
    return value


def _backend_snapshot(
    account: str,
    previous_following: tuple[Member, ...] = (),
) -> Snapshot:
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
        raise CheckerError("Наш collector не смог получить данные. Снимок не сохранён.")

    if not isinstance(payload, dict):
        raise CheckerError("Collector вернул данные в неверном формате.")
    try:
        returned_account = username(payload.get("account"))
    except DataError:
        raise CheckerError("Collector не указал аккаунт снимка.") from None
    if returned_account != account:
        raise CheckerError("Collector вернул данные другого аккаунта. Снимок не сохранён.")

    followers = _parse_members(payload.get("followers"))
    following_visible = _parse_members(payload.get("following"))

    followers_count = _count(payload.get("followers_count"))
    following_count = _count(payload.get("following_count"))

    followers_complete = payload.get("followers_complete")
    if followers_complete is None:
        followers_complete = payload.get("complete") is True
    if followers_complete is not True or len(followers) != followers_count:
        raise CheckerError(
            "Instagram вернул неполный список подписчиков. Снимок не сохранён."
        )

    captured_at = payload.get("captured_at")
    if not isinstance(captured_at, str):
        raise CheckerError("Collector не вернул время снимка.")

    samples = [Sample("followers", "", followers, "id")]
    source = str(payload.get("source") or "pulse-direct-server")

    following_complete = payload.get("following_complete")
    if following_complete is None:
        following_complete = payload.get("complete") is True

    # Only currently observed IDs belong in the current following sample.
    # Equal counters cannot prove that old hidden IDs are still followed.
    try:
        samples.append(Sample(
            "following", "", following_visible, "id",
            complete=following_complete, expected_count=following_count,
        ))
    except DataError as exc:
        raise CheckerError(str(exc)) from None

    return Snapshot(account, captured_at, tuple(samples), source)


def collect_snapshot(
    account: str,
    session_data: object | None = None,
    previous_following: tuple[Member, ...] = (),
) -> Snapshot:
    account = username(account)

    # First try a completely anonymous request from the phone's own network.
    # No Instagram login, cookies, password, or user session are used.
    try:
        return collect_snapshot_direct(account)
    except InstagramDirectError:
        pass

    # If Instagram does not expose the public relationship list anonymously,
    # fall back to our own server-side collector. The user still never signs in.
    return _backend_snapshot(account, previous_following)
