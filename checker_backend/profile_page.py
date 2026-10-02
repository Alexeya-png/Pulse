"""Read profile identity and exact counts from an ordinary public profile page."""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit


def is_profile_url(url: str, target: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.netloc in {"instagram.com", "www.instagram.com"}
        and parsed.path.rstrip("/").lower() == "/" + target
    )


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.metadata: dict[str, list[str]] = {}
        self.canonicals: list[str] = []
        self.payloads: list[object] = []
        self._script = False
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            key = attrs.get("property") or attrs.get("name")
            if key and attrs.get("content") is not None:
                self.metadata.setdefault(key, []).append(attrs["content"])
        elif tag == "link" and "canonical" in (attrs.get("rel") or "").split():
            self.canonicals.append(attrs.get("href") or "")
        elif tag == "script":
            self._script = attrs.get("type") == "application/json"
            self._parts = []

    def handle_data(self, data):
        if self._script:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._script:
            try:
                self.payloads.append(json.loads("".join(self._parts)))
            except (ValueError, RecursionError):
                pass
            self._script = False
            self._parts = []


def _objects(payloads):
    pending = list(payloads)
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            yield value
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)


def parse_profile_page(text: str, target: str, final_url: str) -> dict | None:
    """Reject login pages, wrong identities, ambiguous IDs and rounded counts."""
    if not is_profile_url(final_url, target):
        return None
    page = _Page()
    page.feed(text)
    urls = page.canonicals + page.metadata.get("og:url", [])
    if not urls or not all(is_profile_url(url, target) for url in urls):
        return None

    number = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)"
    pattern = re.compile(
        rf"^\s*({number})\s+Followers\s*,\s*({number})\s+Following(?:\s*,|\s*$)",
        re.IGNORECASE,
    )
    counts = set()
    for description in page.metadata.get("og:description", []):
        match = pattern.match(description)
        if match:
            counts.add(tuple(int(item.replace(",", "")) for item in match.groups()))
    if len(counts) != 1:
        return None

    ids = set()
    privacy = set()
    for value in _objects(page.payloads):
        # Route props identify the displayed profile. Generic IDs elsewhere in
        # the document can belong to the viewer, media owners or recommendations.
        logging = value.get("page_logging")
        if isinstance(logging, dict) and logging.get("name") == "profilePage":
            params = logging.get("params")
            if isinstance(params, dict):
                profile_id = str(params.get("profile_id") or "")
                if (
                    profile_id.isascii() and profile_id.isdecimal()
                    and int(profile_id) > 0
                    and str(value.get("id")) == profile_id
                    and params.get("page_id") == "profilePage_" + profile_id
                ):
                    ids.add(profile_id)
        # Older page payloads contain a full user object instead of route props.
        if str(value.get("username") or "").lower() == target:
            profile_id = str(value.get("pk") or value.get("id") or "")
            if profile_id.isascii() and profile_id.isdecimal() and int(profile_id) > 0:
                ids.add(profile_id)
                if type(value.get("is_private")) is bool:
                    privacy.add(value["is_private"])
    if len(ids) != 1 or len(privacy) > 1:
        return None
    followers, following = counts.pop()
    return {
        "id": ids.pop(),
        "username": target,
        "followers_count": followers,
        "following_count": following,
        # Missing privacy metadata must not be reported as a known public flag.
        "is_private": next(iter(privacy), None),
    }
