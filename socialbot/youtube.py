"""Receive public activity via YouTube's liveChatMessages.list REST endpoint.

The first page contains recent history. Only subsequent pages produce events.
The broadcaster supplies the active live chat ID and a YouTube Data API key.
"""

import http.client
import json
import time
from collections.abc import Iterator
from urllib.parse import urlencode

MAX_PAGE_BYTES = 4 * 1024 * 1024


class FeedError(RuntimeError):
    """YouTube error without request credentials or response bodies."""


def _get_page(live_chat_id: str, api_key: str, page_token: str | None) -> dict:
    query = {"part": "id,snippet,authorDetails", "liveChatId": live_chat_id, "maxResults": 200}
    if page_token is not None:
        query["pageToken"] = page_token
    target = "/youtube/v3/liveChat/messages?" + urlencode(query)
    client = http.client.HTTPSConnection("www.googleapis.com", timeout=30)
    try:
        client.request("GET", target, headers={"X-Goog-Api-Key": api_key, "Accept": "application/json"})
        response = client.getresponse()
        if response.status != 200:
            raise FeedError(f"YouTube live chat returned HTTP {response.status}. Check the chat ID, API access and quota.")
        data = response.read(MAX_PAGE_BYTES + 1)
    except (http.client.HTTPException, OSError):
        raise FeedError("Could not read the YouTube live chat.") from None
    finally:
        client.close()
    if len(data) > MAX_PAGE_BYTES:
        raise FeedError("YouTube response exceeded the size limit.")
    try:
        page = json.loads(data)
    except (ValueError, UnicodeError, RecursionError):
        raise FeedError("Invalid JSON from YouTube.") from None
    if not isinstance(page, dict) or not isinstance(page.get("items"), list):
        raise FeedError("Invalid YouTube live chat response.")
    return page


def _normalize(item: object, channel: str, live_chat_id: str) -> dict | None:
    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
        return None
    author, snippet = item.get("authorDetails"), item.get("snippet")
    if not isinstance(author, dict) or not isinstance(snippet, dict):
        return None
    if snippet.get("liveChatId") != live_chat_id or author.get("isChatOwner") is True:
        return None
    user_id, user = author.get("channelId"), author.get("displayName")
    if not all(isinstance(v, str) and v.strip() for v in (user, user_id)):
        return None
    method = snippet.get("type")
    if method == "textMessageEvent":
        details = snippet.get("textMessageDetails")
        text = details.get("messageText") if isinstance(details, dict) else None
        if not isinstance(text, str) or not text.strip():
            return None
        kind = "chat"
    elif method in ("superChatEvent", "superStickerEvent"):
        label = "Super Chat" if method == "superChatEvent" else "Super Sticker"
        details = snippet.get("superChatDetails" if method == "superChatEvent" else "superStickerDetails")
        if not isinstance(details, dict):
            return None
        amount = details.get("amountDisplayString")
        text = f"{label} ({amount})" if isinstance(amount, str) and amount.strip() else label
        kind = "gift"
    else:
        return None
    return {"id": item["id"], "platform": "youtube", "channel": channel, "kind": kind,
            "user": user, "user_id": user_id, "text": text, "live_chat_id": live_chat_id}


def iter_events(channel: str, live_chat_id: str, api_key: str, *, once: bool = False) -> Iterator[dict]:
    """Poll an active chat. once=True reads one fresh page after initial history."""
    if not all(isinstance(v, str) and v.strip() for v in (channel, live_chat_id)):
        raise FeedError("Provide a channel label and an active YouTube live chat ID.")
    if not isinstance(api_key, str) or not api_key or not all(33 <= ord(c) < 127 for c in api_key):
        raise FeedError("Provide a valid YouTube Data API key.")
    from .ops import pulse
    cursor = None
    history_ids = set()
    while True:
        page = _get_page(live_chat_id, api_key, cursor)
        pulse()
        if cursor is None:
            history_ids = {i["id"] for i in page["items"] if isinstance(i, dict) and isinstance(i.get("id"), str)}
        else:
            for item in page["items"]:
                event = _normalize(item, channel, live_chat_id)
                if event is not None and event["id"] not in history_ids:
                    yield event
            if once:
                return
        if page.get("offlineAt") or any(
            isinstance(i, dict) and isinstance(i.get("snippet"), dict) and
            i["snippet"].get("type") == "chatEndedEvent" for i in page["items"]
        ):
            return
        cursor = page.get("nextPageToken")
        wait_ms = page.get("pollingIntervalMillis")
        if not isinstance(cursor, str) or not cursor or type(wait_ms) is not int or not 0 <= wait_ms < 2**32:
            raise FeedError("YouTube omitted a valid continuation token or poll interval.")
        time.sleep(max(1, wait_ms / 1000))
