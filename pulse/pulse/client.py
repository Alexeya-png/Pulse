"""Our direct, read-only Instagram web client. No Instagram SDK or proxy service.

Uses the session created in this app's Android WebView. These endpoints belong
to Instagram's website, not to Meta's supported third-party developer API.
"""
from __future__ import annotations

import json
import re
import time
from http.cookies import SimpleCookie, CookieError
from threading import Event

import requests
from requests.adapters import HTTPAdapter

from .errors import SyncError
from .model import username

ORIGIN = "https://www.instagram.com"
COOKIE_NAMES = {"sessionid", "csrftoken", "ds_user_id", "mid", "ig_did", "rur"}
MAX_BODY = 8 * 1024 * 1024
WEB_APP_ID = "936619743392459"
# A strict endpoint allowlist prevents credentials being sent to arbitrary URLs.
ENDPOINT = re.compile(r"(?:users/[a-z0-9_.]{1,30}/usernameinfo/|users/[0-9]{1,40}/info/|accounts/current_user/|friendships/[0-9]{1,40}/(?:followers|following)/|feed/user/[0-9]{1,40}/|media/[0-9]{1,40}/(?:info|likers)/)\Z")


def parse_login_cookies(header: str) -> dict[str, str]:
    if not isinstance(header, str) or len(header) > 32768 or "\n" in header or "\r" in header:
        raise SyncError("Некорректные данные сессии.", "auth")
    try:
        jar = SimpleCookie()
        jar.load(header)
        cookies = {key: item.value for key, item in jar.items() if key in COOKIE_NAMES}
    except CookieError:
        raise SyncError("Не удалось прочитать сессию Instagram.", "auth") from None
    validate_cookies(cookies)
    return cookies


def validate_cookies(cookies):
    if not isinstance(cookies, dict) or not {"sessionid", "csrftoken", "ds_user_id"} <= cookies.keys():
        raise SyncError("Вход в Instagram ещё не завершён.", "auth")
    for key, value in cookies.items():
        if key not in COOKIE_NAMES or not isinstance(value, str) or not value or len(value) > 8192 or any(ord(c) < 32 or ord(c) > 126 for c in value):
            raise SyncError("Некорректные данные сессии.", "auth")
    if not re.fullmatch(r"[0-9]{1,40}", cookies["ds_user_id"]):
        raise SyncError("Не удалось определить аккаунт сессии.", "auth")


class InstagramClient:
    def __init__(self, *, session=None, cancel=None, delay=1.5):
        self.http = session or requests.Session()
        self.http.mount("https://", HTTPAdapter(max_retries=0))
        self.http.headers.update({"Accept": "application/json", "Referer": ORIGIN + "/", "X-IG-App-ID": WEB_APP_ID, "X-Requested-With": "XMLHttpRequest"})
        self.cancel = cancel or Event()
        self.delay = delay
        self.last_request = 0.0
        self.account = None
        self.user_id = None

    def set_settings(self, settings):
        if not isinstance(settings, dict) or settings.get("version") != 2:
            raise SyncError("Формат подключения обновлён. Войдите в Instagram заново; история сохранена.", "auth")
        cookies = settings.get("cookies")
        validate_cookies(cookies)
        agent = settings.get("user_agent", "")
        if not isinstance(agent, str) or not agent or len(agent) > 1024 or any(ord(c) < 32 or ord(c) > 126 for c in agent):
            raise SyncError("Некорректные данные браузерной сессии.", "auth")
        self.http.cookies.clear()
        for key, value in cookies.items():
            self.http.cookies.set(key, value, domain=".instagram.com", path="/", secure=True)
        self.http.headers.update({"User-Agent": agent, "X-CSRFToken": cookies["csrftoken"]})
        self.user_id = cookies["ds_user_id"]
        self.account = username(settings["account"]) if settings.get("account") else None

    def get_settings(self):
        # Only retain Instagram cookies; the client never follows cross-site redirects.
        cookies = {c.name: c.value for c in self.http.cookies if c.name in COOKIE_NAMES and c.domain.lstrip(".") in {"instagram.com", "www.instagram.com"}}
        validate_cookies(cookies)
        return {"version": 2, "cookies": cookies, "user_agent": self.http.headers["User-Agent"], "account": self.account}

    def verify_session(self):
        result = self.request("accounts/current_user/", {"edit": "true"})
        user = result.get("user")
        if not isinstance(user, dict) or str(user.get("pk", user.get("id", ""))) != self.user_id:
            raise SyncError("Instagram не подтвердил аккаунт сессии.", "auth")
        self.account = username(user.get("username"))
        return self.account

    def request(self, endpoint: str, params=None):
        if not ENDPOINT.fullmatch(endpoint):
            raise SyncError("Запрос к этому адресу не разрешён.", "data")
        if not self.user_id:
            raise SyncError("Подключите аккаунт Instagram.", "auth")
        if self.cancel.wait(max(0, self.delay - (time.monotonic() - self.last_request))):
            raise SyncError("Проверка отменена.", "cancelled")
        self.last_request = time.monotonic()
        profile_match = re.fullmatch(r"users/([a-z0-9_.]{1,30})/usernameinfo/", endpoint)
        if profile_match:
            endpoint = "users/web_profile_info/"
            params = {"username": profile_match[1]}
        try:
            response = self.http.get(ORIGIN + "/api/v1/" + endpoint, params=params or {}, timeout=(10, 20), allow_redirects=False, stream=True)
            try:
                if response.status_code == 429:
                    raise SyncError("Instagram ограничил запросы. Следующая попытка — через 24 часа.", "rate_limit")
                if response.status_code == 401 or 300 <= response.status_code < 400:
                    raise SyncError("Сессия истекла. Войдите в Instagram заново.", "auth")
                if response.status_code == 404:
                    raise SyncError("Профиль или публикация недоступны.", "unavailable")
                if response.status_code >= 500:
                    raise SyncError("Instagram временно недоступен. Повторите позже.")
                raw = bytearray()
                for chunk in response.iter_content(65536):
                    if self.cancel.is_set():
                        raise SyncError("Проверка отменена.", "cancelled")
                    raw.extend(chunk)
                    if len(raw) > MAX_BODY:
                        raise SyncError("Ответ Instagram слишком велик. Проверка остановлена.", "data")
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeError, RecursionError):
                    raise SyncError("Instagram не вернул список. Возможно, нужен повторный вход.", "auth") from None
                if not isinstance(data, dict):
                    raise SyncError("Изменился формат ответа Instagram.", "data")
                message = str(data.get("message", "")).lower()
                if data.get("challenge") or data.get("checkpoint_url") or "challenge" in message or "checkpoint" in message:
                    raise SyncError("Подтвердите вход на странице Instagram, затем подключитесь снова.", "challenge")
                if "wait" in message or "feedback_required" in message or "rate" in message:
                    raise SyncError("Instagram ограничил запросы. Следующая попытка — через 24 часа.", "rate_limit")
                if response.status_code == 403 or "login_required" in message:
                    raise SyncError("Instagram отклонил доступ. Подключите аккаунт заново.", "auth")
                if response.status_code != 200 or data.get("status") != "ok":
                    raise SyncError("Instagram отклонил запрос. Данные не обновлены.", "data")
                if profile_match:
                    return self._profile(data)
                return data
            finally:
                response.close()
        except requests.RequestException:
            raise SyncError("Ошибка сети. Предыдущие списки сохранены.") from None

    @staticmethod
    def _profile(data):
        payload = data.get("data")
        if payload is not None and not isinstance(payload, dict):
            raise SyncError("Изменился формат ответа Instagram. Данные не обновлены.", "data")
        user = (payload or {}).get("user")
        if not isinstance(user, dict):
            raise SyncError("Профиль недоступен.", "unavailable")
        # Preserve missing counts as None; missing data must never become zero.
        def count(name):
            edge = user.get(name)
            return edge.get("count") if isinstance(edge, dict) else None
        return {"status": "ok", "user": {"pk": user.get("id"), "username": user.get("username"), "follower_count": count("edge_followed_by"), "following_count": count("edge_follow")}}

    def close(self):
        self.http.cookies.clear()
        self.http.close()


def make_client(**kwargs):
    return InstagramClient(**kwargs)
