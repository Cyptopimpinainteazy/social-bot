"""Read Twitch chat over authenticated TLS IRC; send a single reviewed reply via Helix."""

from http.client import HTTPException
import json
import os
import re
import socket
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

IRC_HOST = "irc.chat.twitch.tv"
LOGIN = re.compile(r"[a-z0-9_]{1,25}")
MESSAGE_ID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", re.I)
MAX_LINE = 65536


class TwitchError(RuntimeError):
    """A configuration, connection or explicit API rejection."""


class TwitchPreflightError(TwitchError):
    """Authorization failed before a send request was made."""


class TwitchDeliveryUnknown(TwitchError):
    """A send was attempted, but Twitch's outcome was not confirmed."""


def _login(value):
    value = value.lower()
    if not LOGIN.fullmatch(value):
        raise TwitchError("Supply a Twitch login containing letters, digits or underscores.")
    return value


def _token(variable):
    value = os.getenv(variable, "").strip().removeprefix("oauth:")
    if not value or not value.isascii() or any(ord(ch) < 33 or ord(ch) > 126 for ch in value):
        raise TwitchError(f"Set {variable} to a valid Twitch user access token.")
    return value


def _request_json(request, *, sending=False):
    unknown = TwitchDeliveryUnknown if sending else TwitchError
    try:
        with urlopen(request, timeout=15) as response:
            return json.load(response)
    except HTTPError as exc:
        if sending and exc.code >= 500:
            raise TwitchDeliveryUnknown(f"Twitch returned HTTP {exc.code} after the send attempt.") from None
        raise TwitchError(f"Twitch returned HTTP {exc.code}. Check authorization and rate limits.") from None
    except (URLError, OSError, HTTPException, ValueError) as exc:
        raise unknown(f"Twitch did not return a usable response ({type(exc).__name__}).") from None


def _validate(token, scope, account):
    request = Request("https://id.twitch.tv/oauth2/validate")
    request.add_unredirected_header("Authorization", f"OAuth {token}")
    identity = _request_json(request)
    if (not isinstance(identity, dict) or identity.get("login", "").lower() != account
            or scope not in (identity.get("scopes") or [])
            or not identity.get("client_id") or not identity.get("user_id")):
        raise TwitchError(f"The token must belong to {account} and include {scope}.")
    return identity


def parse_event(line, channel, bot_login):
    """Return one native chat, cheer or individual gift-sub event from the selected room."""
    if not line.startswith("@"):
        return None
    header, _, line = line.partition(" ")
    tags = dict(field.split("=", 1) for field in header[1:].split(";") if "=" in field)
    parts = line.split(" ", 3)
    if len(parts) < 3:
        return None
    prefix, command, target, *tail = parts
    if target != f"#{channel}" or not MESSAGE_ID.fullmatch(tags.get("id", "")):
        return None
    if tags.get("source-room-id") and tags["source-room-id"] != tags.get("room-id"):
        return None  # A copy of a message posted to another channel in a Shared Chat.
    native_id = tags.get("user-id", "")
    if not native_id.isascii() or not native_id.isdigit():
        return None
    if command == "PRIVMSG":
        user = prefix.lstrip(":").split("!", 1)[0].lower()
        text = tail[0].removeprefix(":") if tail else ""
        kind = "chat"
        bits = tags.get("bits", "")
        if bits.isascii() and bits.isdigit() and bits.strip("0"):
            kind, text = "gift", f"{bits} Bits"
    elif command == "USERNOTICE" and tags.get("msg-id") == "subgift":
        user = tags.get("login", "").lower()
        kind, text = "gift", "a gift subscription"
    else:
        return None
    if not LOGIN.fullmatch(user) or user in (bot_login, channel, "jtv", "twitchnotify") or not text.strip():
        return None
    return {"id": tags["id"], "platform": "twitch", "channel": channel, "kind": kind,
            "user": user, "user_id": native_id, "text": text}


def _session(channel, bot_login, token):
    context = ssl.create_default_context()
    next_validation = time.monotonic() + 3300
    with socket.create_connection((IRC_HOST, 6697), timeout=15) as raw:
        with context.wrap_socket(raw, server_hostname=IRC_HOST) as stream, stream.makefile("rb") as lines:
            stream.settimeout(360)
            stream.sendall((f"PASS oauth:{token}\r\nNICK {bot_login}\r\n"
                            "CAP REQ :twitch.tv/tags twitch.tv/commands\r\n").encode())
            while True:
                if time.monotonic() >= next_validation:
                    _validate(token, "chat:read", bot_login)
                    next_validation = time.monotonic() + 3300
                data = lines.readline(MAX_LINE + 1)
                if not data:
                    return
                if len(data) > MAX_LINE or not data.endswith(b"\n"):
                    raise TwitchError("Incomplete or oversized IRC line received.")
                line = data.decode("utf-8").rstrip("\r\n")
                # Parse the command position so viewer text cannot impersonate server notices.
                head = line.partition(" ")[2] if line.startswith("@") else line
                head = head.partition(" ")[2] if head.startswith(":") else head
                command, _, args = head.partition(" ")
                if command == "PING":
                    stream.sendall(f"PONG {args}\r\n".encode())
                elif command == "001":
                    stream.sendall(f"JOIN #{channel}\r\n".encode())
                elif command == "RECONNECT":
                    return
                elif command == "NOTICE" and (args.startswith("* ") or
                        line.startswith(("@msg-id=msg_banned ", "@msg-id=msg_channel_suspended "))):
                    raise TwitchError("Twitch refused the IRC login or channel access.")
                elif command == "CAP" and " NAK " in args:
                    raise TwitchError("Twitch refused the IRC capabilities required for stable event IDs.")
                else:
                    event = parse_event(line, channel, bot_login)
                    if event:
                        yield event


def iter_twitch_events(channel: str):
    """Yield live events and reconnect briefly after transient network interruptions."""
    channel = _login(channel)
    login = _login(os.environ.get("TWITCH_BOT_LOGIN", ""))
    token = _token("TWITCH_OAUTH_TOKEN")
    failures = 0
    while True:
        _validate(token, "chat:read", login)
        started = time.monotonic()
        try:
            yield from _session(channel, login, token)
        except ssl.SSLCertVerificationError as exc:
            raise TwitchError("Twitch TLS certificate verification failed.") from exc
        except OSError:
            pass  # No event replay is available across an IRC disconnection.
        if time.monotonic() - started >= 60:
            failures = 0
        failures += 1
        if failures >= 5:
            raise TwitchError("Twitch IRC disconnected repeatedly. Check connectivity and restart the listener.")
        time.sleep(2 ** (failures - 1))


def send_twitch_reply(channel: str, parent_id: str, text: str) -> str:
    """Send exactly one reply as the owner of this channel; return Twitch's receipt ID."""
    channel = _login(channel)
    if not MESSAGE_ID.fullmatch(parent_id):
        raise TwitchError("A Twitch parent message UUID is required.")
    if not text.strip() or len(text) > 500 or any(ord(ch) < 32 for ch in text):
        raise TwitchError("A Twitch reply must be one line with 1–500 characters.")
    try:
        token = _token("TWITCH_OWNER_TOKEN")
        owner = _validate(token, "user:write:chat", channel)
    except TwitchError as exc:
        raise TwitchPreflightError(str(exc)) from None
    body = {"broadcaster_id": owner["user_id"], "sender_id": owner["user_id"],
            "reply_parent_message_id": parent_id, "message": text}
    request = Request("https://api.twitch.tv/helix/chat/messages", data=json.dumps(body).encode(),
                      headers={"Client-Id": owner["client_id"], "Content-Type": "application/json"})
    request.add_unredirected_header("Authorization", f"Bearer {token}")
    result = _request_json(request, sending=True)
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise TwitchDeliveryUnknown("No Twitch delivery receipt. Check the channel before any retry.")
    receipt = data[0]
    if receipt.get("is_sent") is False:
        reason = receipt.get("drop_reason") or {}
        code = reason.get("code", "unspecified") if isinstance(reason, dict) else "unspecified"
        if code == "automod_held":
            raise TwitchDeliveryUnknown("Twitch held this reply for moderation. Check the channel before any retry.")
        raise TwitchError(f"Twitch declined this message ({code}).")
    if receipt.get("is_sent") is not True or not receipt.get("message_id"):
        raise TwitchDeliveryUnknown("Incomplete Twitch delivery receipt. Check the channel before any retry.")
    return receipt["message_id"]
