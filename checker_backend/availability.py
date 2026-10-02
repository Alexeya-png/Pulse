"""Shared backoff for the single collector process, without credential logging."""
from __future__ import annotations

import math
import threading
import time
from datetime import timezone
from email.utils import parsedate_to_datetime

from fastapi import HTTPException


class Availability:
    def __init__(self):
        self._lock = threading.Lock()
        self._until = 0.0
        self._detail = ""

    def check(self):
        with self._lock:
            remaining = math.ceil(self._until - time.monotonic())
            detail = self._detail
        if remaining > 0:
            raise HTTPException(
                503, f"{detail} Повторная проверка доступна через {remaining} сек.",
                headers={"Retry-After": str(remaining)},
            )

    def stop(self, detail: str, seconds: int):
        with self._lock:
            self._until = max(self._until, time.monotonic() + max(1, seconds))
            self._detail = detail
        self.check()


def retry_after_seconds(value: object, default: int = 900) -> int:
    """Accept both HTTP-date and delay-seconds; never shorten a server delay."""
    if not isinstance(value, str):
        return default
    value = value.strip()
    if value.isascii() and value.isdecimal():
        return max(1, int(value))
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(1, math.ceil(date.timestamp() - time.time()))
    except (TypeError, ValueError, OverflowError):
        return default


availability = Availability()
