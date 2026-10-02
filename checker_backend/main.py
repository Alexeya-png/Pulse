from __future__ import annotations

import logging
import os
import re
import threading
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from checker_backend.direct_instagram import collect_direct_snapshot
from checker_backend.availability import availability

app = FastAPI(title="Pulse Checker", version="0.7.5")

USERNAME_RE = re.compile(r"^[a-z0-9_.]{1,30}$")
COLLECTION_TIMEOUT = max(30, min(210, int(os.environ.get("COLLECTION_TIMEOUT", "180"))))
_collect_lock = threading.Lock()
logger = logging.getLogger("uvicorn.error")


class CollectRequest(BaseModel):
    username: str


def _normal_username(value: str) -> str:
    value = (value or "").strip().removeprefix("@").lower()
    if not USERNAME_RE.fullmatch(value):
        raise HTTPException(400, "Некорректный Instagram-ник.")
    return value


@app.get("/health")
def health():
    return {
        "ok": True,
        "engine": "pulse-direct-instagram",
        "session_configured": bool(os.environ.get("IG_SESSION_JSON", "").strip()),
        "third_party_scraper": False,
        "hiker_dependency": False,
    }


@app.post("/v1/collect")
def collect(request: CollectRequest):
    target = _normal_username(request.username)
    if not _collect_lock.acquire(blocking=False):
        raise HTTPException(429, "Сейчас уже выполняется другая проверка. Попробуйте позже.")
    try:
        try:
            availability.check()
            return collect_direct_snapshot(
                target,
                time.monotonic() + COLLECTION_TIMEOUT,
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("Direct collector failed (%s)", exc.__class__.__name__)
            raise HTTPException(
                503,
                "Не удалось выполнить прямой сбор Instagram. Снимок не сохранён.",
            ) from exc
    finally:
        _collect_lock.release()
