"""Receive verified Kick webhooks for one creator behind an HTTPS reverse proxy."""

import base64
import binascii
from collections import OrderedDict
from datetime import datetime
from hashlib import blake2b
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import re
import sys
import time
from urllib.request import Request, urlopen

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

PUBLIC_KEY_URL = "https://api.kick.com/public/v1/public-key"
PATH = "/webhooks/kick"
BODY_LIMIT = 65536
ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}", re.I)
RFC3339 = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?(?:Z|[+-]\d\d:\d\d)")
ACTORS = {"chat.message.sent": "sender", "channel.followed": "follower",
          "channel.subscription.gifts": "gifter", "kicks.gifted": "sender"}


class KickError(ValueError):
    """A rejected delivery or local configuration error."""


class KickKeyUnavailable(RuntimeError):
    """The trusted endpoint could not supply a usable key."""


def _fetch_public_key():
    request = Request(PUBLIC_KEY_URL, headers={"Accept": "application/json", "User-Agent": "social-bot/1"})
    try:
        with urlopen(request, timeout=5) as response:
            if response.geturl() != PUBLIC_KEY_URL:
                raise ValueError("Unexpected key origin")
            raw = response.read(16385)
        if len(raw) > 16384:
            raise ValueError("Oversized key response")
        # The API reference uses a JSON envelope. Kick's verification example also shows PEM.
        if not raw.startswith(b"-----BEGIN PUBLIC KEY-----"):
            raw = json.loads(raw)["data"]["public_key"].encode("ascii")
        key = serialization.load_pem_public_key(raw)
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
            raise ValueError("Unusable RSA key")
        return key
    except (OSError, HTTPException, AttributeError, KeyError, TypeError, ValueError, UnsupportedAlgorithm):
        raise KickKeyUnavailable("Could not load Kick's current webhook public key.") from None


class KeyCache:
    """Refresh at expiry or after a signature mismatch, at most once a minute."""

    def __init__(self):
        self.key = None
        self.last_attempt = float("-inf")
        self.expires = 0

    def get(self, refresh=False):
        now = time.monotonic()
        if self.key is not None and now < self.expires and not refresh:
            return self.key
        if now - self.last_attempt >= 60:
            self.last_attempt = now
            self.key = _fetch_public_key()
            self.expires = now + 300
        if self.key is None or now >= self.expires:
            raise KickKeyUnavailable("Kick webhook key temporarily unavailable.")
        return self.key

    def verify(self, signature, signed):
        key = self.get()
        try:
            key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            refreshed = self.get(refresh=True)
            if refreshed is not key:
                try:
                    refreshed.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
                    return
                except InvalidSignature:
                    pass
            raise KickError("Invalid Kick signature") from None


def _string(value, limit):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit or
            any(ord(ch) < 32 for ch in value)):
        raise KickError("Invalid field in Kick delivery")
    return value


def verify_event(headers, body, channel, broadcaster_id, keys):
    """Authenticate the raw delivery, select this broadcaster and normalize supported events."""
    event_id = headers.get("Kick-Event-Message-Id", "")
    timestamp = headers.get("Kick-Event-Message-Timestamp", "")
    event_type = headers.get("Kick-Event-Type", "")
    signature = headers.get("Kick-Event-Signature", "")
    if (len(body) > BODY_LIMIT or not ULID.fullmatch(event_id) or
            not RFC3339.fullmatch(timestamp) or len(signature) > 2048):
        raise KickError("Invalid Kick envelope")
    try:
        sent = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
        signature_bytes = base64.b64decode(signature, validate=True)
    except (ValueError, OverflowError, binascii.Error):
        raise KickError("Invalid Kick timestamp or signature encoding") from None
    if abs(time.time() - sent) > 300:
        raise KickError("Kick delivery outside the five-minute window")
    signed = b".".join((event_id.encode("ascii"), timestamp.encode("ascii"), body))
    keys.verify(signature_bytes, signed)
    if headers.get("Kick-Event-Version") != "1" or event_type not in ACTORS:
        return None
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise KickError("Invalid signed JSON payload") from None
    if not isinstance(data, dict):
        raise KickError("Invalid Kick event")
    broadcaster = data.get("broadcaster")
    if not isinstance(broadcaster, dict) or type(broadcaster.get("user_id")) is not int:
        raise KickError("Missing Kick broadcaster")
    if str(broadcaster["user_id"]) != broadcaster_id or broadcaster.get("channel_slug") != channel:
        raise KickError("Wrong Kick broadcaster")
    actor = data.get(ACTORS[event_type])
    if not isinstance(actor, dict):
        raise KickError("Missing Kick event actor")
    if actor.get("is_anonymous") is True or actor.get("user_id") == broadcaster["user_id"]:
        return None
    if type(actor.get("user_id")) is not int or actor["user_id"] <= 0:
        raise KickError("Missing Kick actor ID")
    user = _string(actor.get("username"), 128)
    if event_type == "chat.message.sent":
        kind, text = "chat", _string(data.get("content"), 4096)
        event_id = _string(data.get("message_id"), 128)
    elif event_type == "channel.followed":
        kind, text = "follow", ""
    elif event_type == "channel.subscription.gifts":
        recipients = data.get("giftees")
        if not isinstance(recipients, list) or not recipients:
            raise KickError("Missing gift recipients")
        kind, text = "gift", f"{len(recipients)} gift subscription(s)"
    else:
        gift = data.get("gift")
        if not isinstance(gift, dict) or type(gift.get("amount")) is not int or gift["amount"] <= 0:
            raise KickError("Missing Kick gift amount")
        kind, text = "gift", f"{_string(gift.get('name'), 200)} ({gift['amount']} KICKs)"
    return {"id": event_id, "platform": "kick", "channel": channel, "kind": kind,
            "user": user, "user_id": str(actor["user_id"]), "text": text}


def _handler(channel, broadcaster_id, keys, consume):
    class Receiver(BaseHTTPRequestHandler):
        timeout = 5
        server_version = "social-bot"

        def log_message(self, format, *args):
            pass  # The body and query may contain viewer data.

        def finish_status(self, status):
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            if self.path != PATH:
                return self.finish_status(404)
            lengths = self.headers.get_all("Content-Length", [])
            if (self.headers.get("Transfer-Encoding") or len(lengths) != 1
                    or not re.fullmatch(r"[0-9]{1,8}", lengths[0])):
                return self.finish_status(400)
            length = int(lengths[0])
            if not 0 < length <= BODY_LIMIT:
                return self.finish_status(413)
            for header in ("Kick-Event-Message-Id", "Kick-Event-Message-Timestamp", "Kick-Event-Type",
                           "Kick-Event-Version", "Kick-Event-Signature"):
                if len(self.headers.get_all(header, [])) != 1:
                    return self.finish_status(400)
            try:
                body = self.rfile.read(length)
                if len(body) != length:
                    return self.finish_status(400)
                event = verify_event(self.headers, body, channel, broadcaster_id, keys)
            except TimeoutError:
                return self.finish_status(408)
            except KickError:
                return self.finish_status(400)
            except KickKeyUnavailable:
                return self.finish_status(503)
            if event is not None:
                try:
                    consume(event)
                except Exception:
                    # The caller owns persistence. Leave unsuccessful deliveries eligible for retry.
                    return self.finish_status(503)
            self.finish_status(204)
    return Receiver


def serve_kick(channel, consume, port=8091):
    """Serve POST /webhooks/kick on loopback. Consume runs synchronously on the caller thread."""
    channel = channel.lower()
    broadcaster_id = os.environ.get("KICK_BROADCASTER_ID", "")
    if not re.fullmatch(r"[a-z0-9_-]{1,128}", channel) or not re.fullmatch(r"[1-9][0-9]{0,19}", broadcaster_id):
        raise KickError("Supply a Kick channel slug and set KICK_BROADCASTER_ID to its numeric ID.")
    keys = KeyCache()
    keys.get()
    with HTTPServer(("127.0.0.1", port), _handler(channel, broadcaster_id, keys, consume)) as server:
        server.serve_forever()


def listen_kick(channel, rules, port=8091):
    """Show anonymous per-event suggestions in the creator's console without opening a database."""
    seen = OrderedDict()
    counts = {"chat": 0, "follow": 0, "gift": 0}

    def preview(event):
        now = time.monotonic()
        while seen and next(iter(seen.values())) <= now:
            seen.popitem(last=False)
        digest = blake2b(event["id"].encode(), digest_size=16).digest()
        if digest in seen:
            return
        if len(seen) >= 4096:
            seen.popitem(last=False)
        seen[digest] = now + 300

        kind = event["kind"]
        counts[kind] += 1
        if kind == "chat":
            template = next((reply for regex, reply in rules["keywords"] if regex.search(event["text"])), None)
        else:
            template = rules[kind]
        line = f"Kick {kind} · {counts[kind]} this session"
        if template:
            # Only operator-written text enters the terminal, even if a viewer types a command or handle.
            suggestion = template.replace("@{user}", "{user}").format(user="friend")
            line += f"  |  Suggested reply: {suggestion}"
        if sys.stdout.isatty():
            print("\r\033[2K" + line, end="", flush=True)
        else:
            print(line, flush=True)

    try:
        serve_kick(channel, preview, port)
    finally:
        if sys.stdout.isatty():
            print()
