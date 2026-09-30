"""Strict, bounded JSON/ZIP imports. ZIP members are never extracted."""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from .model import DataError, Member, Sample, Snapshot, username

MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_TOTAL_JSON_BYTES = 128 * 1024 * 1024
MAX_ZIP_ENTRIES = 20_000
MAX_MEMBERS = 500_000
FOLLOWER_FILE = re.compile(r"followers(?:_([1-9][0-9]*))?\.json\Z")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DataError("JSON содержит повторяющиеся ключи.")
        result[key] = value
    return result


def _read_json(stream: BinaryIO):
    raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise DataError("Один JSON-файл превышает лимит 32 МиБ.")
    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, DataError):
            raise
        raise DataError("Не удалось прочитать JSON. Выберите выгрузку в формате JSON, не HTML.") from None


def _export_members(data) -> list[Member]:
    if isinstance(data, dict) and set(data) == {"relationships_followers"}:
        data = data["relationships_followers"]
    if not isinstance(data, list) or len(data) > MAX_MEMBERS:
        raise DataError("Ожидается список подписчиков из выгрузки Instagram.")
    members = []
    for entry in data:
        if not isinstance(entry, dict):
            raise DataError("Повреждённая запись подписчика; импорт отменён.")
        strings = entry.get("string_list_data")
        if not isinstance(strings, list) or len(strings) != 1 or not isinstance(strings[0], dict):
            raise DataError("Неизвестный формат записи подписчика; импорт отменён.")
        # The nested timestamp is the follow date, NOT the snapshot date.
        members.append(Member(strings[0].get("value")))
    return members


def _deduplicate(members: list[Member]) -> tuple[Member, ...]:
    if len(members) > MAX_MEMBERS:
        raise DataError("Лимит одного списка: 500 000 записей.")
    return tuple({m.key: m for m in members}.values())


def _custom_sample(data, kind: str, subject: str = "") -> Sample:
    if not isinstance(data, dict) or data.get("complete") is not True:
        raise DataError("Для каждого списка требуется complete: true. Неполные списки не сравниваются.")
    identity = data.get("identity", "username")
    users = data.get("users")
    if not isinstance(users, list) or len(users) > MAX_MEMBERS:
        raise DataError("users должен быть списком не более чем из 500 000 пользователей.")
    members = []
    for value in users:
        if isinstance(value, str):
            members.append(Member(value))
        elif isinstance(value, dict) and set(value) <= {"username", "id"}:
            members.append(Member(value.get("username"), value.get("id")))
        else:
            raise DataError("Некорректная запись пользователя в users.")
    members = _deduplicate(members)
    expected = data.get("expected_count")
    if expected is not None and (type(expected) is not int or expected != len(members)):
        raise DataError("expected_count не совпадает с числом уникальных пользователей. Список может быть неполным.")
    return Sample(kind, subject, members, identity)


def _custom(data: dict, account: str) -> Snapshot:
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise DataError("Поддерживается schema_version: 1.")
    if username(data.get("account")) != username(account):
        raise DataError("Аккаунт в снимке отличается от выбранного аккаунта.")
    samples = []
    if "followers" in data:
        samples.append(_custom_sample(data["followers"], "followers"))
    if "media" in data:
        if not isinstance(data["media"], list):
            raise DataError("media должен быть списком публикаций.")
        for media in data["media"]:
            if not isinstance(media, dict):
                raise DataError("Некорректная запись публикации.")
            samples.append(_custom_sample(media, "likes", media.get("post_id")))
    return Snapshot(account, data.get("captured_at"), tuple(samples))


def load_snapshot(path: str | Path, account: str, captured_at: str, *, export_complete: bool = False) -> Snapshot:
    """Custom snapshots use their own timestamp; Meta exports need user metadata."""
    path = Path(path)
    account = username(account)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise DataError("Файл превышает лимит 512 МиБ. Запросите выгрузку без фото и видео.")
    try:
        if zipfile.is_zipfile(path):
            return _load_zip(path, account, captured_at, export_complete)
        with path.open("rb") as stream:
            data = _read_json(stream)
        if isinstance(data, dict) and "schema_version" in data:
            return _custom(data, account)
        if not export_complete:
            raise DataError("Подтвердите, что выбраны все части списка подписчиков за всё время.")
        if not FOLLOWER_FILE.fullmatch(path.name):
            raise DataError("Для выгрузки нужен followers.json, followers_1.json или ZIP со всеми частями. liked_posts.json содержит ваши лайки другим людям.")
        match = FOLLOWER_FILE.fullmatch(path.name)
        if match.group(1) not in (None, "1"):
            raise DataError("Это отдельная часть списка. Импортируйте ZIP со всеми followers_N.json.")
        return Snapshot(account, captured_at, (Sample("followers", "", _deduplicate(_export_members(data))),), "instagram_export")
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError):
        raise DataError("ZIP повреждён, зашифрован или использует неподдерживаемое сжатие.") from None


def _load_zip(path: Path, account: str, captured_at: str, complete: bool) -> Snapshot:
    if not complete:
        raise DataError("Подтвердите полноту выгрузки: все подписчики, все части, период «За всё время».")
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise DataError("В ZIP слишком много файлов. Запросите только подписчиков, без медиа.")
        selected = []
        for info in infos:
            name = PurePosixPath(info.filename.replace("\\", "/"))
            match = FOLLOWER_FILE.fullmatch(name.name)
            if not match:
                continue
            if name.is_absolute() or ".." in name.parts or ":" in str(name):
                raise DataError("Недопустимый путь внутри ZIP.")
            if info.file_size > MAX_JSON_BYTES or info.flag_bits & 1:
                raise DataError("Часть выгрузки слишком велика или зашифрована.")
            selected.append((info, name, int(match.group(1) or 0)))
        if not selected:
            raise DataError("В ZIP нет followers_N.json. Выберите JSON-выгрузку подписчиков.")
        if len({str(name.parent) for _, name, _ in selected}) != 1:
            raise DataError("В ZIP несколько папок подписчиков. Импортируйте один аккаунт за раз.")
        numbers = sorted(n for _, _, n in selected)
        if numbers != [0] and numbers != list(range(1, len(numbers) + 1)):
            raise DataError("Нарушена последовательность followers_N.json: пропущена или повторена часть.")
        if sum(info.file_size for info, _, _ in selected) > MAX_TOTAL_JSON_BYTES:
            raise DataError("Списки подписчиков в ZIP превышают лимит 128 МиБ.")
        members = []
        for info, _, _ in sorted(selected, key=lambda row: row[2]):
            with archive.open(info) as stream:
                members.extend(_export_members(_read_json(stream)))
            if len(members) > MAX_MEMBERS:
                raise DataError("Лимит списка: 500 000 записей.")
    return Snapshot(account, captured_at, (Sample("followers", "", _deduplicate(members)),), "instagram_export")
