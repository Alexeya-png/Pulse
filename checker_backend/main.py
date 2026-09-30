from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Pulse Checker", version="0.3.0")

USERNAME_RE = re.compile(r"^[a-z0-9_.]{1,30}$")
MAX_MEMBERS = int(os.environ.get("MAX_MEMBERS", "500000"))
HIKER_BASE_URL = os.environ.get("HIKER_BASE_URL", "https://api.hikerapi.com").rstrip("/")

_collect_lock = threading.Lock()


class CollectRequest(BaseModel):
    username: str


def _normal_username(value: str) -> str:
    value = (value or "").strip().removeprefix("@").lower()
    if not USERNAME_RE.fullmatch(value):
        raise HTTPException(400, "Некорректный Instagram-ник.")
    return value


def _api_key() -> str:
    key = os.environ.get("HIKER_API_KEY", "").strip()
    if not key:
        raise HTTPException(
            503,
            "Сервис проверки ещё не настроен: добавьте HIKER_API_KEY в Render.",
        )
    return key


def _get(path: str, params: dict):
    try:
        response = requests.get(
            HIKER_BASE_URL + path,
            params=params,
            headers={
                "x-access-key": _api_key(),
                "accept": "application/json",
                "user-agent": "PulseChecker/0.3",
            },
            timeout=(10, 60),
        )
    except requests.RequestException:
        raise HTTPException(502, "Сервис Instagram-данных временно недоступен.") from None

    if response.status_code in (401, 403):
        raise HTTPException(
            503,
            "HikerAPI отклонил API-ключ. Проверьте HIKER_API_KEY в Render.",
        )
    if response.status_code == 404:
        raise HTTPException(404, "Instagram-аккаунт не найден.")
    if response.status_code == 429:
        raise HTTPException(
            429,
            "Лимит запросов HikerAPI временно исчерпан. Попробуйте позже.",
        )
    if response.status_code >= 500:
        raise HTTPException(502, "Сервис Instagram-данных временно недоступен.")
    if response.status_code >= 400:
        try:
            detail = response.json()
        except Exception:
            detail = None
        message = None
        if isinstance(detail, dict):
            message = detail.get("detail") or detail.get("message") or detail.get("error")
        raise HTTPException(
            502,
            str(message or f"HikerAPI вернул ошибку {response.status_code}."),
        )

    try:
        data = response.json()
    except ValueError:
        raise HTTPException(502, "Сервис Instagram-данных вернул повреждённый ответ.") from None

    return data


def _profile(username: str) -> dict:
    data = _get("/v1/user/by/username", {"username": username})
    if not isinstance(data, dict):
        raise HTTPException(502, "HikerAPI вернул неожиданный ответ профиля.")
    if not data.get("pk"):
        raise HTTPException(404, "Instagram-аккаунт не найден.")
    return data


def _member(row: dict) -> dict:
    if not isinstance(row, dict):
        raise HTTPException(502, "Сервис вернул некорректный элемент списка.")
    user_id = row.get("pk") or row.get("id")
    value = row.get("username")
    if user_id is None or not value:
        raise HTTPException(502, "Сервис вернул неполные данные пользователя.")
    return {"id": str(user_id), "username": str(value).lower()}


def _collect_pages(path: str, user_id: str, expected: int, label: str) -> list[dict]:
    if expected > MAX_MEMBERS:
        raise HTTPException(413, f"{label}: список больше лимита сервера.")

    result: dict[str, dict] = {}
    max_id = None
    seen_cursors = set()

    while True:
        params = {"user_id": user_id}
        if max_id:
            params["max_id"] = max_id

        page = _get(path, params)

        # HikerAPI v1 chunk endpoints currently return either:
        #   [users, next_max_id]
        # or, on some deployments, {"users": [...], "next_max_id": "..."}.
        if isinstance(page, list) and len(page) >= 1:
            users = page[0]
            next_max_id = page[1] if len(page) > 1 else None
        elif isinstance(page, dict):
            users = page.get("users")
            next_max_id = page.get("next_max_id")
        else:
            raise HTTPException(502, f"{label}: HikerAPI вернул неожиданный формат страницы.")

        if not isinstance(users, list):
            raise HTTPException(502, f"{label}: сервис вернул неполную страницу.")

        for row in users:
            member = _member(row)
            result[member["id"]] = member
            if len(result) > MAX_MEMBERS:
                raise HTTPException(413, f"{label}: список больше лимита сервера.")

        if not next_max_id:
            break

        next_max_id = str(next_max_id)
        if next_max_id in seen_cursors:
            raise HTTPException(502, f"{label}: зациклилась пагинация. Снимок не сохранён.")
        seen_cursors.add(next_max_id)
        max_id = next_max_id

        # Keep pressure low on the upstream API for large accounts.
        time.sleep(0.03)

    if len(result) != expected:
        raise HTTPException(
            409,
            f"{label}: получен неполный список ({len(result)} из {expected}). Снимок не сохранён.",
        )
    return list(result.values())


@app.get("/health")
def health():
    return {
        "ok": True,
        "engine": "hikerapi",
        "api_key_configured": bool(os.environ.get("HIKER_API_KEY")),
    }


@app.post("/v1/collect")
def collect(request: CollectRequest):
    target = _normal_username(request.username)

    if not _collect_lock.acquire(blocking=False):
        raise HTTPException(429, "Сейчас уже выполняется другая проверка. Попробуйте позже.")

    try:
        before = _profile(target)

        if bool(before.get("is_private")):
            raise HTTPException(
                403,
                "Этот режим поддерживает только публичные Instagram-аккаунты.",
            )

        user_id = str(before["pk"])
        before_followers = int(before.get("follower_count") or 0)
        before_following = int(before.get("following_count") or 0)

        followers = _collect_pages(
            "/v1/user/followers/chunk",
            user_id,
            before_followers,
            "Подписчики",
        )
        following = _collect_pages(
            "/v1/user/following/chunk",
            user_id,
            before_following,
            "Подписки",
        )

        after = _profile(target)
        if str(after.get("pk")) != user_id:
            raise HTTPException(
                409,
                "Аккаунт изменился во время проверки. Запустите сбор ещё раз.",
            )

        after_followers = int(after.get("follower_count") or 0)
        after_following = int(after.get("following_count") or 0)
        if after_followers != before_followers or after_following != before_following:
            raise HTTPException(
                409,
                "Списки изменились прямо во время проверки. Запустите сбор ещё раз.",
            )

        return {
            "account": target,
            "account_id": user_id,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "followers_count": before_followers,
            "following_count": before_following,
            "followers": followers,
            "following": following,
            "complete": True,
            "source": "hikerapi",
        }
    finally:
        _collect_lock.release()
