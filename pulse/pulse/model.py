from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse


class DataError(ValueError):
    """Invalid or incomparable data; safe to display to the user."""


def username(value: object) -> str:
    if not isinstance(value, str):
        raise DataError("Имя пользователя должно быть строкой.")
    value = value.strip().removeprefix("@").lower()
    if not re.fullmatch(r"[a-z0-9_.]{1,30}", value):
        raise DataError("Некорректное имя пользователя Instagram.")
    return value


def timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise DataError("Укажите дату снимка в формате ISO 8601 с часовым поясом.")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError
        return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, OverflowError):
        raise DataError("Дата должна содержать часовой пояс: 2026-09-29T12:00:00+03:00.") from None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def post_id(value: object) -> str:
    if not isinstance(value, str):
        raise DataError("У публикации должен быть строковый post_id.")
    value = value.strip()
    if value.startswith(("https://", "http://")):
        url = urlparse(value)
        parts = url.path.strip("/").split("/")
        if url.hostname not in {"instagram.com", "www.instagram.com"} or len(parts) != 2 or parts[0] not in {"p", "reel", "tv"}:
            raise DataError("Нужна ссылка на публикацию Instagram или её постоянный идентификатор.")
        value = parts[1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise DataError("Некорректный идентификатор публикации.")
    return value


@dataclass(frozen=True, slots=True)
class Member:
    username: str
    user_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "username", username(self.username))
        if self.user_id is not None and (not isinstance(self.user_id, str) or not re.fullmatch(r"[0-9]{1,40}", self.user_id)):
            raise DataError("user_id должен быть строкой из цифр.")

    @property
    def key(self) -> str:
        return self.user_id if self.user_id is not None else self.username


@dataclass(frozen=True, slots=True)
class Sample:
    kind: str
    subject: str
    members: tuple[Member, ...]
    identity: str = "username"
    complete: bool = True
    expected_count: int | None = None

    def __post_init__(self):
        if self.kind not in {"followers", "following", "likes"} or self.identity not in {"username", "id"}:
            raise DataError("Неизвестный тип списка или идентификатора.")
        if self.kind in {"followers", "following"} and self.subject != "":
            raise DataError("Список подписчиков не должен содержать post_id.")
        if self.kind == "likes":
            object.__setattr__(self, "subject", post_id(self.subject))
        if any((m.user_id is not None) != (self.identity == "id") for m in self.members):
            raise DataError("В одном списке все пользователи должны использовать один тип идентификатора.")
        if len({m.key for m in self.members}) != len(self.members):
            raise DataError("В списке повторяются идентификаторы пользователей.")
        if not isinstance(self.complete, bool):
            raise DataError("Некорректный признак полноты списка.")
        expected = self.expected_count
        if expected is None and self.complete:
            expected = len(self.members)
            object.__setattr__(self, "expected_count", expected)
        if type(expected) is not int or expected < len(self.members):
            raise DataError("Количество пользователей не совпало со счётчиком.")
        if self.complete and expected != len(self.members):
            raise DataError("Полный список не совпал со счётчиком.")
        if not self.complete and (self.kind != "following" or expected <= len(self.members)):
            raise DataError("Неполными могут быть только подписки с известным числом скрытых аккаунтов.")


@dataclass(frozen=True, slots=True)
class Snapshot:
    account: str
    captured_at: str
    samples: tuple[Sample, ...]
    source: str = "snapshot_json"

    def __post_init__(self):
        object.__setattr__(self, "account", username(self.account))
        object.__setattr__(self, "captured_at", timestamp(self.captured_at))
        if not self.samples:
            raise DataError("В файле нет списков для сравнения.")
        if len({(s.kind, s.subject) for s in self.samples}) != len(self.samples):
            raise DataError("Один список или публикация встречается несколько раз.")
