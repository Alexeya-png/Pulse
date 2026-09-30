from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Pulse Checker", version="0.5.3")

USERNAME_RE = re.compile(r"^[a-z0-9_.]{1,30}$")
MAX_MEMBERS = int(os.environ.get("MAX_MEMBERS", "500000"))
APIFY_BASE_URL = os.environ.get("APIFY_BASE_URL", "https://api.apify.com/v2").rstrip("/")
APIFY_PROFILE_ACTOR = os.environ.get(
    "APIFY_PROFILE_ACTOR",
    "apify~instagram-profile-scraper",
)
APIFY_RELATION_ACTOR = os.environ.get(
    "APIFY_RELATION_ACTOR",
    "scraping_solutions~instagram-scraper-followers-following",
)
APIFY_RELATION_FALLBACK_ACTOR = os.environ.get(
    "APIFY_RELATION_FALLBACK_ACTOR",
    "scraping_solutions~instagram-scraper-followers-following-no-cookies",
)
APIFY_PAGE_SIZE = min(1000, max(1, int(os.environ.get("APIFY_PAGE_SIZE", "1000"))))
APIFY_POLL_SECONDS = max(0.2, float(os.environ.get("APIFY_POLL_SECONDS", "1.5")))
APIFY_RUN_TIMEOUT = max(30, int(os.environ.get("APIFY_RUN_TIMEOUT", "150")))
APIFY_RELATION_ATTEMPTS = max(1, min(5, int(os.environ.get("APIFY_RELATION_ATTEMPTS", "3"))))
APIFY_RETRY_DELAY = max(0.0, float(os.environ.get("APIFY_RETRY_DELAY", "2.0")))
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}
_collect_lock = threading.Lock()
logger = logging.getLogger("uvicorn.error")


class CollectRequest(BaseModel):
    username: str


def _normal_username(value: str) -> str:
    value = (value or "").strip().removeprefix("@").lower()
    if not USERNAME_RE.fullmatch(value):
        raise HTTPException(400, "Некорректный Instagram-ник.")
    return value


def _api_token() -> str:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise HTTPException(
            503,
            "Онлайн-collector ещё не настроен: добавьте APIFY_TOKEN в Render.",
        )
    return token


def _request_json(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    params: dict | None = None,
    timeout: tuple[int, int] = (10, 120),
    allow_404: bool = False,
):
    try:
        response = requests.request(
            method,
            APIFY_BASE_URL + path,
            json=body,
            params=params,
            headers={
                "Authorization": f"Bearer {_api_token()}",
                "Accept": "application/json",
                "User-Agent": "PulseChecker/0.5.1",
            },
            timeout=timeout,
        )
    except requests.RequestException:
        raise HTTPException(
            502,
            "Онлайн-сервис Instagram-данных временно недоступен.",
        ) from None

    if allow_404 and response.status_code == 404:
        return None
    if response.status_code in (401, 403):
        raise HTTPException(
            503,
            "Apify отклонил API-токен. Проверьте APIFY_TOKEN в Render.",
        )
    if response.status_code == 402:
        raise HTTPException(
            429,
            "Бесплатный лимит онлайн-collector исчерпан. Дождитесь обновления бесплатных кредитов.",
        )
    if response.status_code == 429:
        raise HTTPException(
            429,
            "Онлайн-collector временно ограничил запросы. Попробуйте позже.",
        )
    if response.status_code >= 500:
        raise HTTPException(
            502,
            "Онлайн-сервис Instagram-данных временно недоступен.",
        )
    if response.status_code >= 400:
        try:
            payload = response.json()
            provider_message = (
                payload.get("error", {}).get("message")
                if isinstance(payload, dict) and isinstance(payload.get("error"), dict)
                else None
            )
        except ValueError:
            provider_message = None
        if provider_message:
            logger.warning(
                "Apify request failed (%s): %s",
                response.status_code,
                str(provider_message)[:400],
            )
        else:
            logger.warning("Apify request failed (%s)", response.status_code)
        raise HTTPException(
            502,
            f"Онлайн-collector вернул ошибку {response.status_code}.",
        )

    try:
        return response.json()
    except ValueError:
        raise HTTPException(
            502,
            "Онлайн-collector вернул повреждённый ответ.",
        ) from None


def _as_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _profile(username: str) -> dict:
    rows = _request_json(
        "POST",
        f"/actors/{APIFY_PROFILE_ACTOR}/run-sync-get-dataset-items",
        body={
            "usernames": [username],
            "includeAboutSection": False,
        },
        params={
            "timeout": 60,
            "memory": 256,
            "maxItems": 1,
        },
        timeout=(10, 90),
    )
    if not isinstance(rows, list) or not rows:
        raise HTTPException(404, "Instagram-аккаунт не найден.")

    row = rows[0]
    if not isinstance(row, dict):
        raise HTTPException(502, "Онлайн-collector вернул неожиданный профиль.")
    if row.get("error"):
        error = str(row.get("error") or "").lower()
        if "not_found" in error or "not found" in error:
            raise HTTPException(404, "Instagram-аккаунт не найден.")
        raise HTTPException(502, "Онлайн-collector не смог получить профиль.")

    returned_username = str(row.get("username") or "").strip().lower()
    if returned_username and returned_username != username:
        raise HTTPException(502, "Онлайн-collector вернул данные другого профиля.")

    user_id = row.get("id") or row.get("userId")
    followers_count = _as_int(row.get("followersCount"))
    following_count = _as_int(row.get("followsCount"))
    if following_count is None:
        following_count = _as_int(row.get("followingCount"))

    if user_id is None or followers_count is None or following_count is None:
        raise HTTPException(
            502,
            "Онлайн-collector не вернул точные счётчики профиля.",
        )

    return {
        "id": str(user_id),
        "username": returned_username or username,
        "followers_count": followers_count,
        "following_count": following_count,
        "is_private": bool(row.get("private") or row.get("isPrivate")),
    }


def _start_actor(actor: str, body: dict) -> dict:
    payload = _request_json(
        "POST",
        f"/actors/{actor}/runs",
        body=body,
        params={
            "timeout": APIFY_RUN_TIMEOUT,
        },
        timeout=(10, 45),
    )
    run = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(run, dict) or not run.get("id"):
        raise HTTPException(502, "Онлайн-collector не запустил сбор.")
    return run


def _wait_run(run_id: str) -> dict:
    deadline = time.monotonic() + APIFY_RUN_TIMEOUT + 15
    while time.monotonic() < deadline:
        payload = _request_json(
            "GET",
            f"/actor-runs/{run_id}",
            timeout=(10, 30),
        )
        run = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(run, dict):
            raise HTTPException(502, "Онлайн-collector потерял состояние сбора.")
        status = str(run.get("status") or "").upper()
        if status in TERMINAL_STATUSES:
            if status != "SUCCEEDED":
                raise HTTPException(
                    502,
                    f"Онлайн-collector завершил сбор со статусом {status}.",
                )
            return run
        time.sleep(APIFY_POLL_SECONDS)
    raise HTTPException(504, "Онлайн-collector слишком долго собирает данные.")


def _dataset_items(dataset_id: str) -> list:
    items = _request_json(
        "GET",
        f"/datasets/{dataset_id}/items",
        params={
            "clean": "true",
            "format": "json",
            "limit": APIFY_PAGE_SIZE,
        },
        timeout=(10, 60),
    )
    if not isinstance(items, list):
        raise HTTPException(502, "Онлайн-collector вернул повреждённый список.")
    return items


def _run_output(run_id: str) -> dict:
    output = _request_json(
        "GET",
        f"/actor-runs/{run_id}/key-value-store/records/OUTPUT",
        timeout=(10, 30),
        allow_404=True,
    )
    return output if isinstance(output, dict) else {}


def _next_token(output: dict, username: str, data_type: str) -> str | None:
    continuations = output.get("continuations")
    if isinstance(continuations, list):
        for item in continuations:
            if not isinstance(item, dict):
                continue
            account = str(item.get("account") or "").strip().lower()
            kind = str(item.get("dataToScrape") or "").strip().lower()
            if account and account != username:
                continue
            if kind and kind != data_type.lower():
                continue
            token = item.get("nextContinuationToken")
            if token:
                return str(token)

    token = output.get("nextContinuationToken")
    return str(token) if token else None


def _member(row: dict, username: str, data_type: str) -> dict:
    if not isinstance(row, dict):
        raise HTTPException(502, "Онлайн-collector вернул некорректный элемент списка.")

    source = str(row.get("username_scrape") or row.get("sourceUsername") or "").strip().lower()
    if source and source != username:
        raise HTTPException(502, "Онлайн-collector смешал данные разных аккаунтов.")

    row_type = str(row.get("type") or row.get("listType") or "").strip().lower()
    if row_type and row_type != data_type.lower():
        raise HTTPException(502, "Онлайн-collector смешал followers и following.")

    user_id = row.get("id") or row.get("userId")
    member_username = str(row.get("username") or "").strip().lower()
    if user_id is None or not USERNAME_RE.fullmatch(member_username):
        raise HTTPException(502, "Онлайн-collector вернул неполные данные пользователя.")
    return {"id": str(user_id), "username": member_username}


def _collect_relation_from_actor(
    actor: str,
    min_limit: int,
    extra_input: dict,
    username: str,
    expected: int,
    data_type: str,
    label: str,
) -> list[dict]:
    found: dict[str, dict] = {}
    continuation: str | None = None
    seen_tokens: set[str] = set()
    max_runs = (expected // APIFY_PAGE_SIZE) + 3

    for _ in range(max_runs):
        remaining = max(1, expected - len(found))
        body = {
            "Account": [username],
            "resultsLimit": max(
                min_limit,
                min(APIFY_PAGE_SIZE, remaining),
            ),
            "dataToScrape": data_type,
            **extra_input,
        }
        if continuation:
            body["continuationToken"] = continuation

        run = _start_actor(actor, body)
        run = _wait_run(str(run["id"]))
        dataset_id = run.get("defaultDatasetId")
        if not dataset_id:
            raise HTTPException(
                502,
                f"{label}: онлайн-collector не создал результат.",
            )

        rows = _dataset_items(str(dataset_id))
        for row in rows:
            member = _member(row, username, data_type)
            found[member["id"]] = member
            if len(found) > expected:
                raise HTTPException(
                    409,
                    f"{label}: список изменился во время проверки. Снимок не сохранён.",
                )

        output = _run_output(str(run["id"]))
        continuation = _next_token(output, username, data_type)
        logger.info(
            "Apify relation actor %s returned %d rows; total %d/%d; continuation=%s",
            actor,
            len(rows),
            len(found),
            expected,
            bool(continuation),
        )

        if not continuation:
            break
        if continuation in seen_tokens:
            raise HTTPException(
                502,
                f"{label}: онлайн-collector повторил страницу. Снимок не сохранён.",
            )
        seen_tokens.add(continuation)

        if len(found) >= expected:
            break
    else:
        raise HTTPException(
            502,
            f"{label}: слишком много страниц. Снимок не сохранён.",
        )

    return list(found.values())


def _collect_relation(
    username: str,
    expected: int,
    data_type: str,
    label: str,
) -> list[dict]:
    if expected > MAX_MEMBERS:
        raise HTTPException(
            413,
            f"{label}: список больше лимита сервера.",
        )
    if expected == 0:
        return []

    providers = [
        (
            APIFY_RELATION_ACTOR,
            50,
            {"enrichProfileDetails": False},
        ),
        (
            APIFY_RELATION_FALLBACK_ACTOR,
            25,
            {},
        ),
    ]
    combined: dict[str, dict] = {}
    seen_actors: set[str] = set()

    for actor, min_limit, extra_input in providers:
        if not actor or actor in seen_actors:
            continue
        seen_actors.add(actor)

        for attempt in range(APIFY_RELATION_ATTEMPTS):
            try:
                result = _collect_relation_from_actor(
                    actor,
                    min_limit,
                    extra_input,
                    username,
                    expected,
                    data_type,
                    label,
                )
            except HTTPException as exc:
                if exc.status_code in (401, 403, 429):
                    raise
                logger.warning(
                    "Apify relation actor %s attempt %d failed (%d)",
                    actor,
                    attempt + 1,
                    exc.status_code,
                )
                result = []

            for member in result:
                combined[member["id"]] = member
            if len(combined) > expected:
                raise HTTPException(
                    409,
                    f"{label}: данные изменились во время повторного сбора. Снимок не сохранён.",
                )
            if len(combined) == expected:
                return list(combined.values())

            logger.warning(
                "Apify relation actor %s attempt %d produced %d rows; combined %d/%d",
                actor,
                attempt + 1,
                len(result),
                len(combined),
                expected,
            )
            if attempt + 1 < APIFY_RELATION_ATTEMPTS and APIFY_RETRY_DELAY:
                time.sleep(APIFY_RETRY_DELAY)

    missing = expected - len(combined)
    if expected >= 50 and missing == 1:
        logger.warning(
            "%s: Instagram exposes %d/%d records; accepting complete accessible list",
            label,
            len(combined),
            expected,
        )
        return list(combined.values())

    raise HTTPException(
        409,
        f"{label}: получено {len(combined)} из {expected}. Снимок не сохранён.",
    )


def _collect_profile(target: str) -> dict:
    before = _profile(target)
    if before["is_private"]:
        raise HTTPException(
            403,
            "Онлайн-режим поддерживает только публичные Instagram-аккаунты.",
        )

    user_id = before["id"]
    followers_count = before["followers_count"]
    following_count = before["following_count"]
    logger.info(
        "Online exact counts resolved (%d followers, %d following)",
        followers_count,
        following_count,
    )

    followers = _collect_relation(
        target,
        followers_count,
        "Followers",
        "Подписчики",
    )
    following = _collect_relation(
        target,
        following_count,
        "Followings",
        "Подписки",
    )

    after = _profile(target)
    if after["id"] != user_id:
        raise HTTPException(
            409,
            "Аккаунт изменился во время проверки. Запустите сбор ещё раз.",
        )
    if (
        after["followers_count"] != followers_count
        or after["following_count"] != following_count
    ):
        raise HTTPException(
            409,
            "Списки изменились прямо во время проверки. Запустите сбор ещё раз.",
        )
    logger.info(
        "Online complete accessible snapshot collected (%d/%d followers, %d/%d following)",
        len(followers),
        after["followers_count"],
        len(following),
        after["following_count"],
    )
    return {
        "account": target,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "followers_count": len(followers),
        "following_count": len(following),
        "followers": followers,
        "following": following,
        "complete": True,
        "source": "apify-online",
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "engine": "apify-online",
        "provider_configured": bool(os.environ.get("APIFY_TOKEN")),
        "hiker_dependency": False,
    }


@app.post("/v1/collect")
def collect(request: CollectRequest):
    target = _normal_username(request.username)
    if not _collect_lock.acquire(blocking=False):
        raise HTTPException(
            429,
            "Сейчас уже выполняется другая проверка. Попробуйте позже.",
        )
    try:
        try:
            return _collect_profile(target)
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("Online collector failed (%s)", exc.__class__.__name__)
            raise HTTPException(
                503,
                f"Не удалось выполнить онлайн-сбор: {exc.__class__.__name__}.",
            ) from exc
    finally:
        _collect_lock.release()
