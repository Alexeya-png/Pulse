"""Selectable HTTP transport; never retry a denied request with another identity."""
from __future__ import annotations

import os

import requests
from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException as CurlRequestException

HttpSession = requests.Session | curl_requests.Session
REQUEST_ERRORS = (requests.RequestException, CurlRequestException)
# Pin the preset as well as the package so an upgrade cannot silently change it.
BROWSER_PRESET = "chrome136"


def configured_transport() -> str:
    name = os.environ.get("IG_HTTP_TRANSPORT", "requests").strip().lower()
    if name not in {"requests", "curl_cffi"}:
        raise ValueError("IG_HTTP_TRANSPORT must be requests or curl_cffi")
    return name


def new_session() -> HttpSession:
    if configured_transport() == "curl_cffi":
        # Keep the preset's coherent browser headers, TLS and HTTP/2 settings.
        # Retry is disabled: access failures are handled by the collector.
        return curl_requests.Session(
            impersonate=BROWSER_PRESET, default_headers=True, retry=0,
        )
    return requests.Session()


def is_browser_session(session: HttpSession) -> bool:
    return isinstance(session, curl_requests.Session)


def session_transport(session: HttpSession) -> str:
    return "curl_cffi" if is_browser_session(session) else "requests"
