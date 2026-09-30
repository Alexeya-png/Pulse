from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse

app = FastAPI(title="Pulse Instagram OAuth")

IG_APP_ID = os.environ.get("IG_APP_ID", "").strip()
IG_APP_SECRET = os.environ.get("IG_APP_SECRET", "").strip()
IG_REDIRECT_URI = os.environ.get("IG_REDIRECT_URI", "").strip()
APP_RETURN_URI = os.environ.get("APP_RETURN_URI", "pulse://oauth").strip()
DB_PATH = Path(os.environ.get("PULSE_OAUTH_DB", "pulse-oauth.sqlite3"))


def require_config() -> None:
    missing = [
        name
        for name, value in (
            ("IG_APP_ID", IG_APP_ID),
            ("IG_APP_SECRET", IG_APP_SECRET),
            ("IG_REDIRECT_URI", IG_REDIRECT_URI),
        )
        if not value
    ]
    if missing:
        raise HTTPException(503, "OAuth backend is not configured: " + ", ".join(missing))


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            profile_json TEXT NOT NULL,
            access_token TEXT NOT NULL,
            expires_at INTEGER,
            created_at INTEGER NOT NULL
        )
        """
    )
    return connection


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def make_state() -> str:
    payload = {
        "ts": int(time.time()),
        "nonce": secrets.token_urlsafe(18),
    }
    raw = b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = b64(hmac.new(IG_APP_SECRET.encode(), raw.encode(), hashlib.sha256).digest())
    return raw + "." + sig


def verify_state(value: str) -> None:
    try:
        raw, supplied = value.split(".", 1)
        expected = b64(
            hmac.new(IG_APP_SECRET.encode(), raw.encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(expected, supplied):
            raise ValueError
        payload = json.loads(unb64(raw))
        ts = int(payload["ts"])
        if abs(int(time.time()) - ts) > 600:
            raise ValueError
    except Exception:
        raise HTTPException(400, "Invalid or expired OAuth state") from None


def app_return(**params) -> RedirectResponse:
    separator = "&" if "?" in APP_RETURN_URI else "?"
    return RedirectResponse(APP_RETURN_URI + separator + urlencode(params))


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/instagram/start")
def instagram_start(return_uri: str = Query(default="")):
    require_config()
    if return_uri and return_uri != APP_RETURN_URI:
        raise HTTPException(400, "Unexpected return URI")

    state = make_state()
    url = "https://www.instagram.com/oauth/authorize?" + urlencode(
        {
            "client_id": IG_APP_ID,
            "redirect_uri": IG_REDIRECT_URI,
            "response_type": "code",
            "scope": "instagram_business_basic",
            "state": state,
        }
    )
    return RedirectResponse(url)


@app.get("/instagram/callback")
def instagram_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    require_config()
    if error:
        return app_return(error="Доступ Instagram не предоставлен.")
    if not code or not state:
        return app_return(error="Instagram не вернул код авторизации.")

    try:
        verify_state(state)

        token_response = requests.post(
            "https://api.instagram.com/oauth/access_token",
            data={
                "client_id": IG_APP_ID,
                "client_secret": IG_APP_SECRET,
                "grant_type": "authorization_code",
                "redirect_uri": IG_REDIRECT_URI,
                "code": code,
            },
            timeout=20,
        )
        token_response.raise_for_status()
        token_payload = token_response.json()
        short_token = token_payload.get("access_token")
        if not short_token:
            raise RuntimeError("No access token")

        long_response = requests.get(
            "https://graph.instagram.com/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": IG_APP_SECRET,
                "access_token": short_token,
            },
            timeout=20,
        )
        long_response.raise_for_status()
        long_payload = long_response.json()
        access_token = long_payload.get("access_token") or short_token
        expires_in = int(long_payload.get("expires_in") or 3600)

        profile_response = requests.get(
            "https://graph.instagram.com/me",
            params={
                "fields": "id,username,account_type",
                "access_token": access_token,
            },
            timeout=20,
        )
        profile_response.raise_for_status()
        profile = profile_response.json()
        if not profile.get("id") or not profile.get("username"):
            raise RuntimeError("Invalid profile")

        session_id = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + expires_in
        with db() as connection:
            connection.execute(
                """
                INSERT INTO sessions
                    (session_id, profile_json, access_token, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    json.dumps(profile, separators=(",", ":")),
                    access_token,
                    expires_at,
                    int(time.time()),
                ),
            )

        return app_return(session=session_id)
    except HTTPException:
        raise
    except Exception:
        return app_return(error="Не удалось завершить вход Instagram.")


@app.get("/instagram/session")
def instagram_session(session_id: str):
    if not session_id or len(session_id) > 256:
        raise HTTPException(400, "Invalid session")

    with db() as connection:
        row = connection.execute(
            """
            SELECT profile_json, expires_at
            FROM sessions
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()

    if not row:
        raise HTTPException(404, "Session not found")

    expires_at = row[1]
    if expires_at is not None and int(expires_at) <= int(time.time()):
        raise HTTPException(401, "Session expired")

    return {
        "profile": json.loads(row[0]),
        "expires_at": expires_at,
    }
