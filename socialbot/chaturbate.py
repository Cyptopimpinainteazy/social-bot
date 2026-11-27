"""Read public room activity from a broadcaster's Chaturbate Events API feed.

The API supplies stable event IDs. Entries without an ID cannot be deduplicated
and are skipped. Cursor URLs contain the token and are kept only in memory.
API reference: https://chaturbate.com/apps/api/docs/rest.html
"""

import http.client
import json
import re
import time
from collections.abc import Iterator
from urllib.parse import urlsplit

HOST = "eventsapi.chaturbate.com"
MAX_PAGE_BYTES = 4 * 1024 * 1024
KINDS = {"chatMessage": "chat", "userEnter": "join", "tip": "gift", "follow": "follow"}


class FeedError(RuntimeError):
    """A feed error whose message is safe to display."""


class _TemporaryError(FeedError):
    pass


def _get_page(target: str) -> dict:
    # A fixed HTTPS peer and origin-form request prevent credential-bearing
    # redirects and avoid leaking the token in a URL-bearing exception.
    client = http.client.HTTPSConnection(HOST, timeout=100)
    try:
        client.request("GET", target, headers={"Accept": "application/json"})
        response = client.getresponse()
        if 500 <= response.status <= 599:
            raise _TemporaryError(f"Events API returned HTTP {response.status}.")
        if response.status != 200:
            raise FeedError(f"Events API returned HTTP {response.status}.")
        data = response.read(MAX_PAGE_BYTES + 1)
    except (http.client.HTTPException, OSError):
        raise _TemporaryError("Could not reach the Events API.") from None
    finally:
        client.close()

    if len(data) > MAX_PAGE_BYTES:
        raise FeedError("Events API response exceeded the size limit.")
    try:
        page = json.loads(data)
    except (ValueError, UnicodeError, RecursionError):
        raise FeedError("Invalid JSON from the Events API.") from None
    if not isinstance(page, dict) or not isinstance(page.get("events"), list):
        raise FeedError("Invalid Events API page.")
    return page


def _cursor_target(url: object, path: str) -> str:
    if not isinstance(url, str) or not url.isascii() or any(
        ord(char) <= 32 or ord(char) == 127 for char in url
    ):
        raise FeedError("Invalid Events API cursor.")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise FeedError("Invalid Events API cursor.") from None
    if (parts.scheme, parts.netloc, parts.path) != ("https", HOST, path) or parts.fragment:
        raise FeedError("Unexpected Events API cursor destination.")
    return path + ("?" + parts.query if parts.query else "")


def _normalize(event: object, channel: str) -> dict | None:
    if not isinstance(event, dict) or not isinstance(event.get("method"), str):
        return None
    kind = KINDS.get(event["method"])
    identifier = event.get("id")
    obj = event.get("object")
    if not kind or not isinstance(identifier, str) or not identifier.strip():
        return None
    if not isinstance(obj, dict) or obj.get("broadcaster") != channel:
        return None
    actor = obj.get("user")
    user = actor.get("username") if isinstance(actor, dict) else None
    if not isinstance(user, str) or not user.strip() or user.casefold() == channel.casefold():
        return None
    text = ""
    if kind == "chat":
        message = obj.get("message")
        text = message.get("message") if isinstance(message, dict) else None
        if not isinstance(text, str) or not text.strip():
            return None
    elif kind == "gift":
        tip = obj.get("tip")
        if not isinstance(tip, dict) or tip.get("isAnon") is not False:
            return None
        tokens = tip.get("tokens")
        if type(tokens) is not int or tokens < 1:
            return None
        text = f"{tokens} token" if tokens == 1 else f"{tokens} tokens"
    return {"platform": "chaturbate", "channel": channel, "id": identifier,
            "kind": kind, "user": user, "text": text}


def iter_events(channel: str, token: str, *, once: bool = False) -> Iterator[dict]:
    """Yield normalized public events. once=True processes one long-poll page."""
    if not isinstance(channel, str) or not re.fullmatch(r"[A-Za-z0-9_]+", channel):
        raise FeedError("Provide a valid broadcaster username.")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~-]+", token) or token in (".", ".."):
        raise FeedError("Provide a valid Events API token.")
    channel = channel.lower()
    path = f"/events/{channel}/{token}/"
    target = path + "?timeout=10"
    failures = 0
    while True:
        try:
            page = _get_page(target)
        except _TemporaryError:
            if failures == 3:
                raise
            time.sleep(2 ** failures)
            failures += 1
            continue
        failures = 0
        target = _cursor_target(page.get("nextUrl"), path)
        for event in page["events"]:
            item = _normalize(event, channel)
            if item is not None:
                yield item
        if once:
            return
