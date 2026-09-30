from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from urllib3.util import Timeout

app = FastAPI(title="Pulse Checker", version="0.6.1")

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
APIFY_SESSION_ACTOR = os.environ.get(
    "APIFY_SESSION_ACTOR",
    "dami_studio~instagram-followers-following-scraper",
)
APIFY_FULL_FOLLOWING_ACTOR = os.environ.get(
    "APIFY_FULL_FOLLOWING_ACTOR",
    "thenetaji~instagram-followers-followings-scraper",
)
APIFY_FULL_FOLLOWERS_ACTOR = os.environ.get(
    "APIFY_FULL_FOLLOWERS_ACTOR",
    "thenetaji~instagram-followers-followings-scraper",
)
APIFY_FREE_FOLLOWING_ACTOR = os.environ.get(
    "APIFY_FREE_FOLLOWING_ACTOR",
    "publicsignallabs~instagram-following",
)
APIFY_OFFICIAL_RELATION_ACTOR = os.environ.get(
    "APIFY_OFFICIAL_RELATION_ACTOR",
    "apify~instagram-followers-following-scraper",
)
APIFY_CODERX_RELATION_ACTOR = os.environ.get(
    "APIFY_CODERX_RELATION_ACTOR",
    "coderx~instagram-followers-following-scraper-no-cookies-login",
)
APIFY_DANEK_RELATION_ACTOR = os.environ.get(
    "APIFY_DANEK_RELATION_ACTOR",
    "danek~instagram-followers-following-scraper",
)
APIFY_SCRAPESMITH_RELATION_ACTOR = os.environ.get(
    "APIFY_SCRAPESMITH_RELATION_ACTOR",
    "scrapesmith~instagram-followers-following-scraper",
)
APIFY_PAGE_SIZE = min(1000, max(1, int(os.environ.get("APIFY_PAGE_SIZE", "1000"))))
APIFY_POLL_SECONDS = max(0.2, float(os.environ.get("APIFY_POLL_SECONDS", "1.5")))
APIFY_RUN_TIMEOUT = max(30, int(os.environ.get("APIFY_RUN_TIMEOUT", "150")))
APIFY_RELATION_ATTEMPTS = max(1, min(5, int(os.environ.get("APIFY_RELATION_ATTEMPTS", "1"))))
APIFY_SESSION_ATTEMPTS = max(1, min(2, int(os.environ.get("APIFY_SESSION_ATTEMPTS", "1"))))
APIFY_RETRY_DELAY = max(0.0, float(os.environ.get("APIFY_RETRY_DELAY", "2.0")))
COLLECTION_TIMEOUT = max(30, min(180, int(os.environ.get("COLLECTION_TIMEOUT", "180"))))
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}
_collect_lock = threading.Lock()
_collection_state = threading.local()
logger = logging.getLogger("uvicorn.error")


class CollectRequest(BaseModel):
    username: str


class CollectionTimeout(HTTPException):
    def __init__(self):
        super().__init__(504, "Онлайн-collector не успел получить полный снимок за отведённое время. Снимок не сохранён.")


def _remaining() -> float:
    deadline = getattr(_collection_state, "deadline", None)
    remaining = COLLECTION_TIMEOUT if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        raise CollectionTimeout()
    return remaining


def _pause(seconds: float) -> None:
    time.sleep(min(seconds, _remaining()))
    _remaining()


def _diagnostic_code(row: dict) -> str:
    # Actors may put diagnostics under error.code rather than top-level code.
    # Only known codes enter logs; never log raw provider messages/cookies.
    error = row.get("error")
    values = [row.get("code"), row.get("errorCode")]
    if isinstance(error, dict):
        values.extend([error.get("code"), error.get("type")])
    elif isinstance(error, str):
        values.append(error)
    values.extend([row.get("message"), row.get("status")])
    known = {"BAD_INPUT", "NOT_FOUND", "NO_RESULTS", "PROVIDER_UNSUPPORTED",
             "PARTIAL", "RATE_LIMITED", "PROVIDER_AUTH", "SESSION_EXHAUSTED",
             "SERVER_ERROR", "NETWORK"}
    for value in values:
        if isinstance(value, str) and value.upper() in known:
            return value.upper()
        if isinstance(value, str) and "rate limit" in value.lower():
            return "RATE_LIMITED"
    return "UNKNOWN"


def _abort_run(run_id: str) -> None:
    try:
        response = requests.post(
            APIFY_BASE_URL + f"/actor-runs/{run_id}/abort",
            headers={"Authorization": f"Bearer {_api_token()}"},
            timeout=Timeout(total=5, connect=2, read=3),
        )
        logger.info("Stopped unfinished provider run (HTTP %d)", response.status_code)
    except (requests.RequestException, HTTPException):
        logger.warning("Could not stop unfinished provider run")


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


def _checker_sessionid() -> str | None:
    raw = os.environ.get("IG_SESSION_JSON", "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("sessionid")
    if value is None and isinstance(payload.get("cookies"), dict):
        value = payload["cookies"].get("sessionid")
    value = str(value or "").strip()
    return value or None


def _request_json(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    params: dict | None = None,
    timeout: tuple[int, int] = (10, 120),
    allow_404: bool = False,
):
    remaining = _remaining()
    try:
        response = requests.request(
            method,
            APIFY_BASE_URL + path,
            json=body,
            params=params,
            headers={
                "Authorization": f"Bearer {_api_token()}",
                "Accept": "application/json",
                "User-Agent": "PulseChecker/0.5.6",
            },
            timeout=Timeout(total=remaining, connect=min(timeout[0], remaining), read=min(timeout[1], remaining)),
        )
    except requests.RequestException:
        raise HTTPException(
            502,
            "Онлайн-сервис Instagram-данных временно недоступен.",
        ) from None

    if allow_404 and response.status_code == 404:
        return None
    if response.status_code in (401, 403):
        error_type = _safe_apify_error_type(response)
        logger.warning(
            "Apify access rejected status=%d type=%s path=%s",
            response.status_code,
            error_type,
            path.split("?")[0],
        )
        quota_markers = ("limit", "quota", "credit", "usage", "balance", "billing", "rental")
        if any(marker in error_type for marker in quota_markers):
            raise HTTPException(
                429,
                "Лимит Apify для онлайн-collector исчерпан или запуск этого Actor недоступен на текущем плане.",
            )
        raise HTTPException(
            503,
            "Apify отклонил доступ к Actor. Проверьте APIFY_TOKEN и доступ плана в Render.",
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
        # Provider error text may echo submitted input, including session cookies.
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


def _safe_apify_error_type(response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return "unknown"
    values = []
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            values.extend([error.get("type"), error.get("code")])
        values.extend([payload.get("type"), payload.get("code")])
    for value in values:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if re.fullmatch(r"[a-z0-9_.-]{1,80}", normalized):
                return normalized
    return "unknown"


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
            "timeout": max(1, min(60, int(_remaining()) - 5)),
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
    remaining = _remaining()
    if remaining < 25:
        raise CollectionTimeout()
    payload = _request_json(
        "POST",
        f"/actors/{actor}/runs",
        body=body,
        params={
            "timeout": max(1, min(APIFY_RUN_TIMEOUT, int(remaining) - 5)),
        },
        timeout=(10, 45),
    )
    run = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(run, dict) or not run.get("id"):
        raise HTTPException(502, "Онлайн-collector не запустил сбор.")
    return run


def _wait_run(run_id: str) -> dict:
    deadline = time.monotonic() + min(APIFY_RUN_TIMEOUT + 15, _remaining())
    finished = False
    try:
        while time.monotonic() < deadline:
            payload = _request_json("GET", f"/actor-runs/{run_id}", timeout=(10, 30))
            run = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(run, dict):
                raise HTTPException(502, "Онлайн-collector потерял состояние сбора.")
            status = str(run.get("status") or "").upper()
            if status in TERMINAL_STATUSES:
                finished = True
                if status != "SUCCEEDED":
                    raise HTTPException(502, f"Онлайн-collector завершил сбор со статусом {status}.")
                return run
            _pause(min(APIFY_POLL_SECONDS, max(0, deadline - time.monotonic())))
        raise CollectionTimeout()
    finally:
        if not finished:
            _abort_run(run_id)


def _dataset_items(dataset_id: str) -> list:
    result = []
    while True:
        items = _request_json(
            "GET",
            f"/datasets/{dataset_id}/items",
            params={
                "clean": "true",
                "format": "json",
                "limit": APIFY_PAGE_SIZE,
                "offset": len(result),
            },
            timeout=(10, 60),
        )
        if not isinstance(items, list):
            raise HTTPException(502, "Онлайн-collector вернул повреждённый список.")
        result.extend(items)
        if len(result) > MAX_MEMBERS + APIFY_PAGE_SIZE:
            raise HTTPException(413, "Список провайдера больше лимита сервера.")
        if len(items) < APIFY_PAGE_SIZE:
            return result


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

    source = str(
        row.get("username_scrape")
        or row.get("sourceUsername")
        or row.get("source_username")
        or ""
    ).strip().lower()
    if source and source != username:
        raise HTTPException(502, "Онлайн-collector смешал данные разных аккаунтов.")

    row_type = str(row.get("type") or row.get("listType") or "").strip().lower()
    expected_type = data_type.strip().lower()
    if expected_type in {"following", "followings"}:
        expected_type = "following"
    elif expected_type in {"follower", "followers"}:
        expected_type = "followers"
    if row_type in {"following", "followings"}:
        row_type = "following"
    elif row_type in {"follower", "followers"}:
        row_type = "followers"
    if row_type and row_type != expected_type:
        raise HTTPException(502, "Онлайн-collector смешал followers и following.")

    user_id = row.get("id") or row.get("userId") or row.get("pk")
    member_username = str(row.get("username") or "").strip().lower()
    if user_id is None or not USERNAME_RE.fullmatch(member_username):
        raise HTTPException(502, "Онлайн-collector вернул неполные данные пользователя.")
    return {"id": str(user_id), "username": member_username}


def _collect_session_actor(
    username: str,
    expected: int,
    data_type: str,
    label: str,
) -> list[dict]:
    if expected > 1000:
        return []

    list_type = "followers" if data_type == "Followers" else "following"
    body = {
        "profiles": [username],
        "listType": list_type,
        "maxItemsPerProfile": expected,
        "requestTimeoutSeconds": 30,
        "maxRunSeconds": 180,
        "maxAttempts": 12,
        "proxyConfiguration": {"useApifyProxy": True},
    }
    if list_type == "following":
        body.update({
            "requestTimeoutSeconds": 20,
            "maxRunSeconds": 90,
            "maxAttempts": 4,
        })
    body["maxRunSeconds"] = max(20, min(body["maxRunSeconds"], int(_remaining()) - 5))
    sessionid = _checker_sessionid()
    if list_type == "following":
        if not sessionid:
            logger.warning(
                "%s: checker session is unavailable for authenticated following provider",
                label,
            )
            return []
        body["sessionCookies"] = [sessionid]

    run = _start_actor(APIFY_SESSION_ACTOR, body)
    run = _wait_run(str(run["id"]))
    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        raise HTTPException(
            502,
            f"{label}: session-provider не создал результат.",
        )

    rows = _dataset_items(str(dataset_id))
    found: dict[str, dict] = {}
    diagnostic_codes: list[str] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("recordType") == "relationship_profile":
            source = str(row.get("sourceUsername") or "").strip().lower()
            row_type = str(row.get("listType") or "").strip().lower()
            if source and source != username:
                continue
            if row_type and row_type != list_type:
                continue
            member = _member(row, username, data_type)
            found[member["id"]] = member
            continue
        if row.get("ok") is False:
            code = _diagnostic_code(row)
            diagnostic_codes.append(code)

    if diagnostic_codes:
        logger.warning(
            "Apify session actor diagnostics for %s: %s",
            label,
            ",".join(sorted(set(diagnostic_codes))),
        )
    logger.info(
        "Apify session actor returned %d/%d for %s",
        len(found),
        expected,
        label,
    )
    return list(found.values())


def _collect_full_relation_actor(
    actor: str,
    username: str,
    expected: int,
    data_type: str,
    label: str,
) -> list[dict]:
    found: dict[str, dict] = {}
    cursor = None
    seen_cursors: set[str] = set()
    for _ in range((expected // APIFY_PAGE_SIZE) + 3):
        body = {
            "username": [username],
            "type": "followers" if data_type == "Followers" else "followings",
            "maxItem": expected - len(found),
            "enrichProfile": False,
            "fullProfileDetails": False,
        }
        if cursor:
            body["resumeCursor"] = cursor
        run = _start_actor(actor, body)
        run = _wait_run(str(run["id"]))
        dataset_id = run.get("defaultDatasetId")
        if not dataset_id:
            raise HTTPException(
                502,
                f"{label}: full-provider не создал результат.",
            )

        rows = _dataset_items(str(dataset_id))
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("cursor") is not None and not row.get("username"):
                continue
            member = _member(row, username, data_type)
            found[member["id"]] = member
            if len(found) > expected:
                raise HTTPException(
                    409,
                    f"{label}: список изменился во время проверки. Снимок не сохранён.",
                )

        if len(found) == expected:
            break
        output = _run_output(str(run["id"]))
        cursor = output.get("resumeCursor")
        if not cursor:
            break
        if not isinstance(cursor, str) or cursor in seen_cursors:
            raise HTTPException(502, f"{label}: full-provider повторил страницу.")
        seen_cursors.add(cursor)

    logger.info(
        "Apify full relation actor returned %d/%d for %s",
        len(found),
        expected,
        label,
    )
    return list(found.values())


def _collect_full_following_actor(username: str, expected: int, label: str) -> list[dict]:
    return _collect_full_relation_actor(
        APIFY_FULL_FOLLOWING_ACTOR, username, expected, "Followings", label,
    )


def _collect_full_followers_actor(username: str, expected: int, label: str) -> list[dict]:
    return _collect_full_relation_actor(
        APIFY_FULL_FOLLOWERS_ACTOR, username, expected, "Followers", label,
    )


def _collect_free_following_actor(
    username: str,
    expected: int,
    label: str,
) -> list[dict]:
    if expected > 1000:
        return []

    run = _start_actor(
        APIFY_FREE_FOLLOWING_ACTOR,
        {
            "Account": [username],
            "resultsLimit": max(25, expected),
        },
    )
    run = _wait_run(str(run["id"]))
    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        raise HTTPException(
            502,
            f"{label}: free-provider не создал результат.",
        )

    rows = _dataset_items(str(dataset_id))
    found: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        member = _member(row, username, "Followings")
        found[member["id"]] = member
        if len(found) > expected:
            raise HTTPException(
                409,
                f"{label}: список изменился во время проверки. Снимок не сохранён.",
            )

    output = _run_output(str(run["id"]))
    outcome = str(output.get("outcome") or "").upper()
    if outcome and outcome not in {"COMPLETED", "NO_RESULTS"}:
        logger.warning(
            "Apify free following actor outcome for %s: %s",
            label,
            outcome[:64],
        )

    logger.info(
        "Apify free following actor returned %d/%d for %s",
        len(found),
        expected,
        label,
    )
    return list(found.values())


def _collect_official_actor(
    username: str,
    expected: int,
    data_type: str,
    label: str,
) -> list[dict]:
    list_type = (
        "followers"
        if data_type.strip().lower() in {"follower", "followers"}
        else "following"
    )
    run = _start_actor(
        APIFY_OFFICIAL_RELATION_ACTOR,
        {
            "usernames": [username],
            "dataToScrape": list_type,
            "resultsLimit": expected,
        },
    )
    run = _wait_run(str(run["id"]))
    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        raise HTTPException(
            502,
            f"{label}: public-provider не создал результат.",
        )

    rows = _dataset_items(str(dataset_id))
    found: dict[str, dict] = {}
    errors: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("error"):
            errors.append(_diagnostic_code(row))
            continue
        member = _member(row, username, data_type)
        found[member["id"]] = member
        if len(found) > expected:
            raise HTTPException(
                409,
                f"{label}: список изменился во время проверки. Снимок не сохранён.",
            )

    if errors:
        logger.warning(
            "Apify public actor diagnostics for %s: %s",
            label,
            ",".join(sorted(set(errors))),
        )
    logger.info(
        "Apify public actor returned %d/%d for %s",
        len(found),
        expected,
        label,
    )
    return list(found.values())


def _collect_coderx_actor(
    username: str,
    expected: int,
    data_type: str,
    label: str,
) -> list[dict]:
    if expected > 1000:
        return []

    list_type = (
        "followers"
        if data_type.strip().lower() in {"follower", "followers"}
        else "following"
    )
    run = _start_actor(
        APIFY_CODERX_RELATION_ACTOR,
        {
            "username": username,
            "scrape_type": list_type,
            "max_items": max(25, min(expected, 1000)),
        },
    )
    run = _wait_run(str(run["id"]))
    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        raise HTTPException(
            502,
            f"{label}: independent-provider не создал результат.",
        )

    rows = _dataset_items(str(dataset_id))
    found: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not row.get("username"):
            continue
        member = _member(row, username, data_type)
        found[member["id"]] = member
        if len(found) > expected:
            raise HTTPException(
                409,
                f"{label}: список изменился во время проверки. Снимок не сохранён.",
            )

    logger.info(
        "Apify independent no-cookie actor returned %d/%d for %s",
        len(found),
        expected,
        label,
    )
    return list(found.values())


def _collect_danek_actor(
    username: str,
    expected: int,
    data_type: str,
    label: str,
) -> list[dict]:
    if not APIFY_DANEK_RELATION_ACTOR:
        return []

    list_type = "followers" if data_type.strip().lower() in {"follower", "followers"} else "following"
    run = _start_actor(
        APIFY_DANEK_RELATION_ACTOR,
        {
            "usernames": [username],
            "maxResultsPerUser": expected,
            "dataToScrape": list_type,
        },
    )
    run = _wait_run(str(run["id"]))
    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        raise HTTPException(502, f"{label}: независимый collector не создал результат.")

    found: dict[str, dict] = {}
    rows = _dataset_items(str(dataset_id))
    for row in rows:
        member = _member(row, username, data_type)
        found[member["id"]] = member
        if len(found) > expected:
            raise HTTPException(
                409,
                f"{label}: список изменился во время проверки. Снимок не сохранён.",
            )

    logger.info(
        "Apify danek fallback returned %d/%d for %s",
        len(found),
        expected,
        label,
    )
    return list(found.values())


def _collect_scrapesmith_actor(
    username: str,
    expected: int,
    data_type: str,
    label: str,
) -> list[dict]:
    if not APIFY_SCRAPESMITH_RELATION_ACTOR:
        return []

    list_type = "followers" if data_type.strip().lower() in {"follower", "followers"} else "following"
    run = _start_actor(
        APIFY_SCRAPESMITH_RELATION_ACTOR,
        {
            "usernames": [username],
            "maxFollowers": expected,
            "mode": list_type,
        },
    )
    run = _wait_run(str(run["id"]))
    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        raise HTTPException(502, f"{label}: резервный collector не создал результат.")

    found: dict[str, dict] = {}
    rows = _dataset_items(str(dataset_id))
    for row in rows:
        member = _member(row, username, data_type)
        found[member["id"]] = member
        if len(found) > expected:
            raise HTTPException(
                409,
                f"{label}: список изменился во время проверки. Снимок не сохранён.",
            )

    logger.info(
        "Apify scrapesmith fallback returned %d/%d for %s",
        len(found),
        expected,
        label,
    )
    return list(found.values())


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

    combined: dict[str, dict] = {}
    normalized_type = data_type.strip().lower()
    is_following = normalized_type in {"following", "followings"}

    if (APIFY_FULL_FOLLOWING_ACTOR if is_following else APIFY_FULL_FOLLOWERS_ACTOR):
        try:
            primary = _collect_full_following_actor if is_following else _collect_full_followers_actor
            result = primary(
                username,
                expected,
                label,
            )
        except HTTPException as exc:
            if isinstance(exc, CollectionTimeout) or exc.status_code in (401, 403, 409, 429):
                raise
            logger.warning(
                "Apify full relation actor failed for %s (%d)",
                label,
                exc.status_code,
            )
            result = []

        for member in result:
            combined[member["id"]] = member
        if len(combined) > expected:
            raise HTTPException(
                409,
                f"{label}: данные изменились во время сбора. Снимок не сохранён.",
            )
        if len(combined) == expected:
            return list(combined.values())

    if is_following and APIFY_FREE_FOLLOWING_ACTOR and expected <= 1000:
        try:
            result = _collect_free_following_actor(
                username,
                expected,
                label,
            )
        except HTTPException as exc:
            if isinstance(exc, CollectionTimeout) or exc.status_code in (401, 403, 409, 429):
                raise
            logger.warning(
                "Apify free following actor failed for %s (%d)",
                label,
                exc.status_code,
            )
            result = []

        for member in result:
            combined[member["id"]] = member
        if len(combined) > expected:
            raise HTTPException(
                409,
                f"{label}: данные изменились во время сбора. Снимок не сохранён.",
            )
        if len(combined) == expected:
            return list(combined.values())

    if is_following and APIFY_OFFICIAL_RELATION_ACTOR:
        try:
            result = _collect_official_actor(
                username,
                expected,
                data_type,
                label,
            )
        except HTTPException as exc:
            if isinstance(exc, CollectionTimeout) or exc.status_code in (401, 403, 409, 429):
                raise
            logger.warning(
                "Apify public actor failed for %s (%d)",
                label,
                exc.status_code,
            )
            result = []

        for member in result:
            combined[member["id"]] = member
        if len(combined) > expected:
            raise HTTPException(
                409,
                f"{label}: данные изменились во время сбора. Снимок не сохранён.",
            )
        if len(combined) == expected:
            return list(combined.values())

    if APIFY_SESSION_ACTOR and expected <= 1000:
        for attempt in range(APIFY_SESSION_ATTEMPTS):
            try:
                result = _collect_session_actor(
                    username,
                    expected,
                    data_type,
                    label,
                )
            except HTTPException as exc:
                if isinstance(exc, CollectionTimeout) or exc.status_code in (401, 403, 409, 429):
                    raise
                logger.warning(
                    "Apify session actor attempt %d failed (%d)",
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
                "Apify session actor attempt %d produced %d rows; combined %d/%d",
                attempt + 1,
                len(result),
                len(combined),
                expected,
            )
            if attempt + 1 < APIFY_SESSION_ATTEMPTS and APIFY_RETRY_DELAY:
                _pause(APIFY_RETRY_DELAY)

    if is_following:
        logger.info(
            "Following still incomplete at %d/%d for %s; trying general relation fallbacks",
            len(combined),
            expected,
            label,
        )

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
                if isinstance(exc, CollectionTimeout) or exc.status_code in (401, 403, 409, 429):
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
                _pause(APIFY_RETRY_DELAY)

    final_fallbacks = [
        ("danek", APIFY_DANEK_RELATION_ACTOR, _collect_danek_actor),
        ("coderx", APIFY_CODERX_RELATION_ACTOR, _collect_coderx_actor),
        ("scrapesmith", APIFY_SCRAPESMITH_RELATION_ACTOR, _collect_scrapesmith_actor),
    ]
    for provider_name, actor, collector in final_fallbacks:
        if not actor or len(combined) >= expected:
            continue
        try:
            result = collector(
                username,
                expected,
                data_type,
                label,
            )
        except HTTPException as exc:
            if isinstance(exc, CollectionTimeout) or exc.status_code in (401, 403, 409, 429):
                raise
            logger.warning(
                "Apify %s fallback failed for %s (%d)",
                provider_name,
                label,
                exc.status_code,
            )
            result = []

        for member in result:
            combined[member["id"]] = member
        if len(combined) > expected:
            raise HTTPException(
                409,
                f"{label}: данные изменились во время сбора. Снимок не сохранён.",
            )
        if len(combined) == expected:
            return list(combined.values())

        logger.warning(
            "Apify %s fallback produced %d rows; combined %d/%d",
            provider_name,
            len(result),
            len(combined),
            expected,
        )

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

    # Apify FREE effectively permits one active Actor run at a time. Running
    # followers and following concurrently causes the second branch to receive
    # 403/authorization-looking errors while the first Actor still owns the slot.
    # Keep one shared request deadline, but serialize the two relationship lists.
    following = _collect_relation(
        target,
        following_count,
        "Followings",
        "Подписки",
    )
    _remaining()
    followers = _collect_relation(
        target,
        followers_count,
        "Followers",
        "Подписчики",
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
    if len(followers) != followers_count or len(following) != following_count:
        raise HTTPException(409, "Получены неполные списки. Снимок не сохранён.")
    _remaining()
    logger.info(
        "Online complete snapshot collected (%d/%d followers, %d/%d following)",
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
        _collection_state.deadline = time.monotonic() + COLLECTION_TIMEOUT
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
        _collection_state.deadline = None
        _collect_lock.release()
