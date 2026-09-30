from __future__ import annotations

import os
import re
import secrets
import time

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from checker_backend.direct_instagram import collect_direct_snapshot

app = FastAPI(title="Pulse Instagram Relay", version="0.1.0")

USERNAME_RE = re.compile(r"^[a-z0-9_.]{1,30}$")
MAX_TIMEOUT = 170


class RelayCollectRequest(BaseModel):
    username: str
    session_json: str
    timeout_seconds: int = 150


def _authorize(authorization: str | None) -> None:
    expected = os.environ.get("IG_RELAY_TOKEN", "").strip()
    if not expected:
        raise HTTPException(503, "Relay token is not configured.")
    supplied = (authorization or "").strip()
    prefix = "Bearer "
    if not supplied.startswith(prefix):
        raise HTTPException(401, "Missing relay authorization.")
    token = supplied[len(prefix):]
    if not secrets.compare_digest(token, expected):
        raise HTTPException(403, "Invalid relay authorization.")


def _username(value: str) -> str:
    value = (value or "").strip().removeprefix("@").lower()
    if not USERNAME_RE.fullmatch(value):
        raise HTTPException(400, "Invalid Instagram username.")
    return value


@app.get("/health")
def health():
    return {"ok": True, "engine": "direct-instagram-relay"}


@app.post("/v1/collect")
def collect(
    request: RelayCollectRequest,
    authorization: str | None = Header(default=None),
):
    _authorize(authorization)
    target = _username(request.username)
    raw_session = (request.session_json or "").strip()
    if not raw_session:
        raise HTTPException(400, "Missing checker session.")
    timeout_seconds = max(30, min(MAX_TIMEOUT, int(request.timeout_seconds)))
    return collect_direct_snapshot(
        target,
        time.monotonic() + timeout_seconds,
        session_json=raw_session,
    )
