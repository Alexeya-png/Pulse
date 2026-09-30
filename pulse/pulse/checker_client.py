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


def _reconcile_following(
    visible: tuple[Member, ...],
    expected: int,
    previous: tuple[Member, ...],
) -> tuple[Member, ...] | None:
    if expected < 0 or len(visible) > expected:
        return None
    if len(visible) == expected:
        return visible
    if len(previous) != expected or not previous:
        return None
    if any(member.user_id is None for member in previous + visible):
        return None

    previous_by_id = {member.user_id: member for member in previous}
    visible_by_id = {member.user_id: member for member in visible}
    if len(previous_by_id) != expected:
        return None

    # Only carry hidden entries forward when every currently visible ID already
    # existed in the last complete local snapshot. If a new visible ID appears,
    # we cannot know which hidden old ID disappeared, so do not guess.
    if not set(visible_by_id).issubset(previous_by_id):
        return None

    merged = dict(previous_by_id)
    merged.update(visible_by_id)
    if len(merged) != expected:
        return None
    return tuple(merged[key] for key in sorted(merged))


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

    followers = _parse_members(payload.get("followers"))
    following_visible = _parse_members(payload.get("following"))

    try:
        followers_count = int(payload.get("followers_count"))
        following_count = int(payload.get("following_count"))
    except (TypeError, ValueError):
        raise CheckerError("Collector вернул данные в неверном формате.") from None

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

    if following_complete is True:
        if len(following_visible) != following_count:
            raise CheckerError(
                "Количество подписок не совпало со счётчиком. Снимок не сохранён."
            )
        samples.append(Sample("following", "", following_visible, "id"))
    else:
        reconciled = _reconcile_following(
            following_visible,
            following_count,
            previous_following,
        )
        if reconciled is not None:
            samples.append(Sample("following", "", reconciled, "id"))
            source += "-local-reconciled"
        else:
            source += "-followers-only"

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
