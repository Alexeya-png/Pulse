"""Read-only private API adapter. No incomplete list is treated as a removal."""
from __future__ import annotations

from dataclasses import dataclass
from threading import Event

from .model import DataError, Member, Sample, Snapshot, username, utc_now
from .errors import SyncError, safe_error
from .client import make_client


@dataclass(frozen=True, slots=True)
class Collection:
    snapshot: Snapshot
    warnings: tuple[str, ...]
    requests: int


class InstagramReader:
    """One sequential reader per worker. Cancellation is checked between requests."""

    def __init__(self, client, cancel: Event | None = None, progress=None, *, request_limit=250):
        self.client = client
        self.cancel = cancel or Event()
        self.progress = progress or (lambda text: None)
        self.request_limit = request_limit
        self.requests = 0

    def request(self, path: str, params=None):
        if self.cancel.is_set():
            raise SyncError("Проверка отменена. Незавершённые данные не сохранены.", "cancelled")
        if self.requests >= self.request_limit:
            raise SyncError("Достигнут лимит запросов за проверку. Неполный список не сохранён.", "budget")
        self.requests += 1
        try:
            result = self.client.request(path, params=params or {})
        except Exception as exc:
            raise safe_error(exc) from None
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise SyncError("Instagram вернул неизвестный формат. Сравнение отменено.", "data")
        return result

    @staticmethod
    def count(data: dict, key: str) -> int:
        value = data.get(key)
        if type(value) is not int or value < 0:
            raise SyncError("Instagram скрыл счётчик: полноту списка проверить нельзя.", "incomplete")
        return value

    def users(self, path: str, expected: int, title: str) -> tuple[Member, ...]:
        found = {}
        cursors = set()
        cursor = ""
        while True:
            params = {"count": 100}
            if cursor:
                params["max_id"] = cursor
            data = self.request(path, params)
            raw = data.get("users")
            if not isinstance(raw, list):
                raise SyncError("Instagram не вернул список пользователей.", "incomplete")
            for user in raw:
                if not isinstance(user, dict) or user.get("pk", user.get("id")) is None:
                    raise SyncError("В списке отсутствует ID пользователя.", "incomplete")
                member = Member(user.get("username"), str(user.get("pk", user.get("id"))))
                found[member.key] = member
            self.progress(f"{title}: {len(found):,} / {expected:,}".replace(",", " "))
            if len(found) > expected:
                raise SyncError("Список изменился во время загрузки. Повторите проверку позже.", "incomplete")
            cursor = data.get("next_max_id") or ""
            if data.get("big_list") is True and not cursor and len(found) < expected:
                raise SyncError("Instagram вернул только часть большого списка.", "incomplete")
            if not cursor:
                break
            if not raw or str(cursor) in cursors:
                raise SyncError("Instagram повторил страницу. Неполный список не сохранён.", "incomplete")
            cursors.add(str(cursor))
        if len(found) != expected:
            raise SyncError("Instagram вернул неполный список. Изменения не вычислялись.", "incomplete")
        return tuple(found.values())

    def collect(self, target: str, *, recent_posts: int = 12) -> Collection:
        if not 0 <= recent_posts <= 24:
            raise DataError("Можно проверять от 0 до 24 последних публикаций.")
        target = username(target)
        profile = self.request(f"users/{target}/usernameinfo/").get("user")
        if not isinstance(profile, dict) or not str(profile.get("pk", "")).isdigit():
            raise SyncError("Профиль недоступен подключённому аккаунту.", "unavailable")
        target_id = str(profile["pk"])
        expected = self.count(profile, "follower_count")
        following_count = self.count(profile, "following_count")
        samples, warnings = [], []
        try:
            followers = self.users(f"friendships/{target_id}/followers/", expected, "Подписчики")
            following = self.users(f"friendships/{target_id}/following/", following_count, "Подписки")
            after = self.request(f"users/{target}/usernameinfo/").get("user", {})
            if self.count(after, "follower_count") != expected or self.count(after, "following_count") != following_count or str(after.get("pk")) != target_id:
                raise SyncError("Подписчики или подписки изменились во время загрузки.", "incomplete")
            samples.append(Sample("followers", "", followers, "id"))
            samples.append(Sample("following", "", following, "id"))
        except SyncError as exc:
            if exc.code != "incomplete":
                raise
            warnings.append("Подписчики и подписки: один из списков неполный или изменился; оба предыдущих списка сохранены.")
        if recent_posts:
            feed = self.request(f"feed/user/{target_id}/", {"count": recent_posts})
            items = feed.get("items")
            if not isinstance(items, list):
                raise SyncError("Не удалось прочитать публикации.", "data")
            # Pinned posts may be included by Instagram; sort returned items by time.
            if any(not isinstance(item, dict) for item in items):
                raise SyncError("Неизвестный формат ленты.", "data")
            items = sorted(items, key=lambda m: m.get("taken_at", 0), reverse=True)[:recent_posts]
            if feed.get("more_available") and len(items) < recent_posts:
                warnings.append(f"Лента вернула только {len(items)} публикаций; остальные не проверялись.")
            for index, media in enumerate(items, 1):
                if not isinstance(media, dict) or not str(media.get("pk", "")).isdigit():
                    raise SyncError("Неизвестный формат публикации.", "data")
                media_id = str(media["pk"])
                self.progress(f"Лайки: публикация {index} из {len(items)}")
                try:
                    before = self.request(f"media/{media_id}/info/").get("items", [])
                    if not before:
                        raise SyncError("Публикация недоступна.", "incomplete")
                    likes_count = self.count(before[0], "like_count")
                    likers = self.users(f"media/{media_id}/likers/", likes_count, "Лайки")
                    after = self.request(f"media/{media_id}/info/").get("items", [])
                    if not after or self.count(after[0], "like_count") != likes_count:
                        raise SyncError("Лайки изменились во время загрузки.", "incomplete")
                    samples.append(Sample("likes", media_id, likers, "id"))
                except SyncError as exc:
                    if exc.code not in {"incomplete", "unavailable"}:
                        raise
                    warnings.append(f"Публикация {media_id}: список лайков недоступен, неполный или изменился.")
        if self.cancel.is_set():
            raise SyncError("Проверка отменена.", "cancelled")
        if not samples:
            raise SyncError("Ни одного полного списка не получено. Предыдущие данные сохранены.", "incomplete")
        return Collection(Snapshot(target, utc_now(), tuple(samples), "instagram_direct"), tuple(warnings), self.requests)
