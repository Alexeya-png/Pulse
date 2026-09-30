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


def _collect_pages(
    path: str,
    user_id: str,
    label: str,
    cursor_param: str,
    cursor_field: str,
) -> list[dict]:
    result: dict[str, dict] = {}
    cursor = None
    seen_cursors = set()

    while True:
        params = {"user_id": user_id}
        if cursor:
            params[cursor_param] = cursor

        page = _get(path, params)

        if isinstance(page, list) and len(page) >= 1:
            users = page[0]
            next_cursor = page[1] if len(page) > 1 else None
        elif isinstance(page, dict):
            users = page.get("users")
            next_cursor = page.get(cursor_field)
        else:
            raise HTTPException(502, f"{label}: HikerAPI вернул неожиданный формат страницы.")

        if not isinstance(users, list):
            raise HTTPException(502, f"{label}: сервис вернул неполную страницу.")

        for row in users:
            member = _member(row)
            result[member["id"]] = member
            if len(result) > MAX_MEMBERS:
                raise HTTPException(413, f"{label}: список больше лимита сервера.")

        if not next_cursor:
            break

        next_cursor = str(next_cursor)
        if next_cursor in seen_cursors:
            raise HTTPException(502, f"{label}: зациклилась пагинация. Снимок не сохранён.")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        time.sleep(0.03)

    return list(result.values())


def _collect_with_fallback(kind: str, user_id: str, expected: int, label: str) -> list[dict]:
    if expected > MAX_MEMBERS:
        raise HTTPException(413, f"{label}: список больше лимита сервера.")

    v1 = _collect_pages(
        f"/v1/user/{kind}/chunk",
        user_id,
        label,
        "max_id",
        "next_max_id",
    )
    if len(v1) == expected:
        return v1

    gql = _collect_pages(
        f"/gql/user/{kind}/chunk",
        user_id,
        label,
        "end_cursor",
        "end_cursor",
    )
    if len(gql) == expected:
        return gql

    return gql if len(gql) >= len(v1) else v1


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

        followers = _collect_with_fallback(
            "followers",
            user_id,
            before_followers,
            "Подписчики",
        )
        following = _collect_with_fallback(
            "following",
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

        if len(followers) != after_followers:
            raise HTTPException(
                409,
                f"Подписчики: получено {len(followers)} из {after_followers}. Снимок не сохранён.",
            )
        if len(following) != after_following:
            raise HTTPException(
                409,
                f"Подписки: получено {len(following)} из {after_following}. Снимок не сохранён.",
            )

        return {
            "account": target,
            "account_id": user_id,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "followers_count": after_followers,
            "following_count": after_following,
            "followers": followers,
            "following": following,
            "complete": True,
            "source": "hikerapi",
        }
    finally:
        _collect_lock.release()
