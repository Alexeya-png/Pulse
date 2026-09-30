from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .oauth_config import BACKEND_BASE_URL, RETURN_URI


class OAuthConfigError(RuntimeError):
    pass


def configured() -> bool:
    return (
        BACKEND_BASE_URL.startswith("https://")
        and "YOUR-BACKEND" not in BACKEND_BASE_URL
    )


def start_login() -> None:
    if not configured():
        raise OAuthConfigError("OAuth ещё не настроен: укажите адрес backend.")
    from jnius import autoclass

    activity = autoclass("org.kivy.android.PythonActivity").mActivity
    intent_cls = autoclass("android.content.Intent")
    uri_cls = autoclass("android.net.Uri")

    url = (
        BACKEND_BASE_URL.rstrip("/")
        + "/instagram/start?"
        + urlencode({"return_uri": RETURN_URI})
    )
    intent = intent_cls(intent_cls.ACTION_VIEW, uri_cls.parse(url))
    activity.startActivity(intent)


def consume_callback() -> str | None:
    from jnius import autoclass

    activity = autoclass("org.kivy.android.PythonActivity").mActivity
    intent = activity.getIntent()
    if intent is None:
        return None
    value = intent.getDataString()
    if not value:
        return None

    parsed = urlparse(str(value))
    expected = urlparse(RETURN_URI)
    if parsed.scheme != expected.scheme or parsed.netloc != expected.netloc:
        return None

    params = parse_qs(parsed.query)
    session_id = (params.get("session") or [""])[0].strip()
    error = (params.get("error") or [""])[0].strip()

    intent.setData(None)

    if error:
        raise RuntimeError(error)
    return session_id or None


def fetch_profile(session_id: str) -> dict:
    if not configured():
        raise OAuthConfigError("OAuth backend не настроен.")
    if not session_id or len(session_id) > 256:
        raise RuntimeError("Некорректная OAuth-сессия.")

    response = requests.get(
        BACKEND_BASE_URL.rstrip("/") + "/instagram/session",
        params={"session_id": session_id},
        timeout=(8, 15),
    )
    if response.status_code != 200:
        raise RuntimeError("Не удалось подтвердить подключение Instagram.")
    data = response.json()
    profile = data.get("profile")
    if not isinstance(profile, dict) or not profile.get("username"):
        raise RuntimeError("Instagram не вернул профиль.")
    return data
