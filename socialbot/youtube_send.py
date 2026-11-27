"""Owner-authorized public YouTube Live chat sending with explicit delivery outcomes."""
import http.client
import json
import os
from urllib.parse import urlencode

from .twitch import (TwitchDeliveryUnknown as DeliveryUnknown, TwitchError as SendRejected,
                     TwitchPreflightError as PreflightError)

HOST = "www.googleapis.com"
MAX_RESPONSE = 512 * 1024


def _request(token, path, body=None):
    sending = body is not None
    outcome_error = DeliveryUnknown if sending else PreflightError
    client = http.client.HTTPSConnection(HOST, timeout=10)
    try:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if sending:
            headers["Content-Type"] = "application/json"
        client.request("POST" if sending else "GET", path,
                       body=json.dumps(body).encode() if sending else None, headers=headers)
        response = client.getresponse()
        if not 200 <= response.status < 300:
            if sending and 400 <= response.status < 500 and response.status != 408:
                raise SendRejected(f"YouTube rejected the message with HTTP {response.status}.")
            raise outcome_error(f"YouTube returned HTTP {response.status}.")
        raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise outcome_error("YouTube response exceeded its size limit.")
        result = json.loads(raw)
    except (OSError, http.client.HTTPException, ValueError, RecursionError):
        raise outcome_error("YouTube did not return a usable response.") from None
    finally:
        client.close()
    if not isinstance(result, dict):
        raise outcome_error("YouTube returned an invalid resource.")
    return result


def _list(token, resource, params):
    pages = set()
    while True:
        page = _request(token, "/youtube/v3/" + resource + "?" + urlencode(params))
        items = page.get("items")
        if not isinstance(items, list):
            raise PreflightError("YouTube could not confirm broadcast ownership.")
        yield from (item for item in items if isinstance(item, dict))
        cursor = page.get("nextPageToken")
        if cursor is None:
            return
        if not isinstance(cursor, str) or not cursor or len(cursor) > 2048 or cursor in pages:
            raise PreflightError("YouTube returned an invalid page token.")
        pages.add(cursor)
        params["pageToken"] = cursor


def _verify_owner(token, live_chat_id):
    # The active-broadcast filter and mine=true are mutually exclusive on
    # liveBroadcasts.list. Confirm the channel against channels.list(mine=true).
    # This avoids reading a creator's entire archive of completed broadcasts.
    selection = {"part":"snippet,status", "broadcastStatus":"active", "broadcastType":"all",
                 "maxResults":50, "fields":"nextPageToken,items(snippet(channelId,liveChatId),status/lifeCycleStatus)"}
    owner = None
    for item in _list(token, "liveBroadcasts", selection):
        info = item.get("snippet")
        status = item.get("status")
        if (isinstance(info, dict) and info.get("liveChatId") == live_chat_id and
                isinstance(status, dict) and status.get("lifeCycleStatus") == "live"):
            owner = info.get("channelId")
            break
    if not isinstance(owner, str) or not owner:
        raise PreflightError("The saved live chat does not match an active broadcast for this account.")
    query = {"part":"id","mine":"true","maxResults":50,"fields":"nextPageToken,items(id)"}
    if not any(item.get("id") == owner for item in _list(token, "channels", query)):
        raise PreflightError("The YouTube token does not own the active broadcast.")
    return owner


def send_youtube_message(live_chat_id, text):
    """Post one approved top-level message to a verified broadcaster's live chat."""
    if (not isinstance(live_chat_id, str) or not 1 <= len(live_chat_id) <= 512
            or not all(33 <= ord(ch) < 127 for ch in live_chat_id)):
        raise PreflightError("A saved YouTube live chat ID is required.")
    if (not isinstance(text, str) or not text.strip() or len(text) > 200
            or any(ord(ch) < 32 or 127 <= ord(ch) <= 159 or 0xD800 <= ord(ch) <= 0xDFFF for ch in text)):
        raise PreflightError("YouTube messages must be one line of 1–200 characters.")
    token = os.getenv("YOUTUBE_OWNER_TOKEN", "")
    if not token or not all(33 <= ord(ch) < 127 for ch in token):
        raise PreflightError("Set YOUTUBE_OWNER_TOKEN to an authorized Google OAuth access token.")
    owner = _verify_owner(token, live_chat_id)
    snippet = {"liveChatId":live_chat_id,"type":"textMessageEvent","textMessageDetails":{"messageText":text}}
    receipt = _request(token, "/youtube/v3/liveChat/messages?part=snippet", {"snippet":snippet})
    message_id = receipt.get("id")
    stored = receipt.get("snippet")
    if (not isinstance(message_id, str) or not message_id.strip() or not isinstance(stored, dict)
            or stored.get("liveChatId") != live_chat_id or stored.get("type") != "textMessageEvent"
            or stored.get("authorChannelId") != owner):
        raise DeliveryUnknown("YouTube did not confirm a message ID, destination and sending channel.")
    return message_id
