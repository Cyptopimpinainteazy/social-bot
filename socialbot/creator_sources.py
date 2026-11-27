"""Read creator-authorized webhooks, account statistics and the Twitch stream directory."""
from hashlib import sha256
import hmac
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import re
import sqlite3
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID

COMMANDS = ("fanvue-webhook", "fanvue-stats", "tango-webhook", "tango-stats", "tiktok-stats", "discover-rooms")
TIKTOK_FIELDS = ("follower_count", "following_count", "likes_count", "video_count")
PATH = "/webhooks/fanvue"
MAX_BODY = 65536
TANGO_TABLE = """CREATE TABLE IF NOT EXISTS tango_receipts(
    creator BLOB NOT NULL, receipt BLOB NOT NULL, kind TEXT NOT NULL, number INTEGER NOT NULL,
    PRIMARY KEY (creator, receipt)
) WITHOUT ROWID"""
TABLE = """CREATE TABLE IF NOT EXISTS fanvue_follow_receipts(
    creator BLOB NOT NULL, receipt BLOB NOT NULL, PRIMARY KEY (creator, receipt)
) WITHOUT ROWID"""


class InvalidSignature(ValueError):
    pass


def _uuid(text):
    if not isinstance(text, str) or len(text) != 36:
        raise ValueError("A valid UUID is required")
    return str(UUID(text))


def _signature(header, body, secret, now, *, version="v0"):
    # During rotation Fanvue appends additional hex MACs without a v0= prefix.
    fields = header.split(",")
    timestamps = []
    macs = []
    continuing = False
    for entry in fields:
        entry = entry.strip()
        if entry.startswith("t="):
            timestamps.append(entry[2:])
            continuing = False
        elif entry.startswith(version + "="):
            macs.append(entry[3:])
            continuing = version == "v0"
        elif "=" not in entry and continuing:
            macs.append(entry)
        else:
            continuing = False
    if (len(timestamps) != 1 or not re.fullmatch("[0-9]{1,12}", timestamps[0])
            or abs(now - int(timestamps[0])) > 300 or not macs):
        raise InvalidSignature("Invalid or stale webhook signature")
    signed = timestamps[0].encode("ascii") + b"." + body
    expected = hmac.new(secret, signed, sha256).hexdigest()
    if not any(re.fullmatch("[0-9a-fA-F]{64}", mac) and hmac.compare_digest(expected, mac.lower())
               for mac in macs):
        raise InvalidSignature("Invalid webhook signature")


def _digest(text):
    return sha256(text.encode("ascii")).digest()


def accept_fanvue(db, creator, body, signature, secret, *, now=None):
    """Process an authenticated follow transactionally; never store the follower or raw payload."""
    if len(body) > MAX_BODY or len(signature) > 2048:
        raise ValueError("Fanvue request too large")
    if signature:
        _signature(signature, body, secret, time.time() if now is None else now)
    try:
        event = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Invalid Fanvue JSON") from None
    if not isinstance(event, dict):
        raise ValueError("Invalid Fanvue payload")
    topic = event.get("type")
    if not signature:
        if topic == "fanvue.webhook.verification" and set(event) <= {"type", "timestamp"}:
            return "probe"
        raise InvalidSignature("Missing Fanvue signature")
    if topic != "creator.follow.created":
        return "ignored"
    data = event.get("data")
    if not isinstance(data, dict) or data.get("object") != "follow" or not isinstance(data.get("creator"), dict):
        raise ValueError("Invalid Fanvue follow")
    owner = data["creator"].get("uuid")
    follower = data.get("follower")
    if not isinstance(owner, str) or not isinstance(follower, dict) or not isinstance(follower.get("uuid"), str):
        raise ValueError("Missing Fanvue event participant")
    if owner.startswith("TEST-") or follower["uuid"].startswith("TEST-"):
        return "test"
    if _uuid(owner) != creator:
        return "ignored"
    _uuid(follower["uuid"])
    receipt_id = _uuid(event.get("id"))
    with db:
        row = db.execute("INSERT INTO fanvue_follow_receipts VALUES (?,?) ON CONFLICT DO NOTHING",
                         (_digest(creator), _digest(receipt_id)))
    return "received" if row.rowcount == 1 else "duplicate"


def accept_tango(db, creator, body, signature, secret, *, now=None):
    """Count new follows and user-authored live room chat. Direct messages never enter storage."""
    if len(body) > MAX_BODY or not signature or len(signature) > 2048:
        raise InvalidSignature("Invalid Tango delivery")
    _signature(signature, body, secret, time.time() if now is None else now, version="v1")
    try:
        event = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Invalid Tango JSON") from None
    if not isinstance(event, dict):
        raise ValueError("Invalid Tango event")
    if event.get("creatorId") != creator or event.get("type") not in ("follower.added", "stream.chat.messages.received"):
        return "ignored"
    data, eid = event.get("data"), event.get("id")
    if not isinstance(data, dict) or not isinstance(eid, str) or not eid.isascii() or not 1 <= len(eid) <= 512:
        raise ValueError("Invalid Tango envelope")
    if event["type"] == "follower.added":
        fan = data.get("follower")
        if not isinstance(fan, dict) or not isinstance(fan.get("id"), str) or not fan["id"]:
            raise ValueError("Missing Tango follower")
        kind, number = "follows", 1
    else:
        messages = data.get("messages")
        if not isinstance(data.get("streamId"), str) or not isinstance(messages, list):
            raise ValueError("Invalid Tango stream batch")
        number = 0
        for message in messages:
            if not isinstance(message, dict) or type(message.get("systemMessage")) is not bool:
                raise ValueError("Invalid Tango stream message")
            if not message["systemMessage"]:
                sender = message.get("sender")
                if (not isinstance(sender, dict) or not isinstance(sender.get("id"), str)
                        or not sender["id"] or not isinstance(message.get("text"), str)):
                    raise ValueError("Invalid Tango viewer message")
                number += 1
        kind = "viewer_chat_messages"
    with db:
        row = db.execute("INSERT INTO tango_receipts VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
                         (_digest(creator), _digest(eid), kind, number))
    return kind if row.rowcount == 1 else "duplicate"


def _receiver(db, creator, secret, *, tango=False):
    path = "/webhooks/tango/" + creator if tango else PATH
    signature_header = "TNG-Webhook-Signature" if tango else "X-Fanvue-Signature"
    accept = accept_tango if tango else accept_fanvue
    class Receiver(BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, format, *args):
            pass  # Avoid persisting query strings, creator or viewer data.

        def status(self, value):
            self.send_response(value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_OPTIONS(self):
            self.status(200 if tango and self.path == "/webhooks/tango" else 404)

        def do_POST(self):
            if self.path != path:
                return self.status(404)
            lengths = self.headers.get_all("Content-Length", [])
            signatures = self.headers.get_all(signature_header, [])
            if (self.headers.get("Transfer-Encoding") or len(lengths) != 1 or len(signatures) > 1
                    or not re.fullmatch(r"[0-9]{1,8}", lengths[0])):
                return self.status(400)
            size = int(lengths[0])
            if not 0 < size <= MAX_BODY:
                return self.status(413)
            try:
                body = self.rfile.read(size)
                if len(body) != size:
                    return self.status(400)
                result = accept(db, creator, body, signatures[0] if signatures else "", secret)
            except TimeoutError:
                return self.status(408)
            except InvalidSignature:
                return self.status(401)
            except ValueError:
                return self.status(400)
            except sqlite3.Error:
                return self.status(503)
            if result in ("received", "test"):
                print("Fanvue follow received" if result == "received" else "Fanvue signed test received", flush=True)
            elif result in ("follows", "viewer_chat_messages"):
                print(f"Tango delivery received ({result})", flush=True)
            self.status(204)
    return Receiver


def tiktok_stats():
    token = os.getenv("TIKTOK_ACCESS_TOKEN", "").strip()
    if not token or not token.isascii() or any(ord(ch) < 33 or ord(ch) > 126 for ch in token):
        raise ValueError("Set TIKTOK_ACCESS_TOKEN to the creator's OAuth token with user.info.stats.")
    url = "https://open.tiktokapis.com/v2/user/info/?fields=" + ",".join(TIKTOK_FIELDS)
    request = Request(url, headers={"Accept": "application/json"})
    request.add_unredirected_header("Authorization", "Bearer " + token)
    try:
        with urlopen(request, timeout=10) as response:
            if response.geturl() != url:
                raise ValueError("Unexpected redirect")
            body = response.read(32769)
        if len(body) > 32768:
            raise ValueError("Response too large")
        result = json.loads(body)
        if result["error"]["code"] != "ok":
            raise ValueError("API error")
        values = {key: result["data"]["user"][key] for key in TIKTOK_FIELDS}
        if any(type(v) is not int or v < 0 for v in values.values()):
            raise ValueError("Invalid count")
        return values
    except HTTPError as exc:
        raise ValueError(f"TikTok returned HTTP {exc.code}; check authorization and rate limits.") from None
    except (OSError, HTTPException, ValueError, TypeError, KeyError):
        raise ValueError("TikTok did not return the four authorized account counts.") from None


def discover_rooms(game_id, language=None, limit=10):
    if not re.fullmatch("[0-9]{1,20}", game_id) or not 1 <= limit <= 100:
        raise ValueError("Use a numeric Twitch category ID and a limit from 1 to 100.")
    params = {"game_id": game_id, "first": limit}
    if language:
        if not re.fullmatch("[a-z]{2}|other", language):
            raise ValueError("Use a two-letter lowercase Twitch language or other.")
        params["language"] = language
    token = os.getenv("TWITCH_DISCOVERY_TOKEN", "").strip()
    client_id = os.getenv("TWITCH_CLIENT_ID", "").strip()
    if not token or not client_id or not all(33 <= ord(c) < 127 for c in token + client_id):
        raise ValueError("Set TWITCH_DISCOVERY_TOKEN and its matching TWITCH_CLIENT_ID.")
    url = "https://api.twitch.tv/helix/streams?" + urlencode(params)
    request = Request(url, headers={"Client-Id": client_id, "Accept": "application/json"})
    request.add_unredirected_header("Authorization", "Bearer " + token)
    try:
        with urlopen(request, timeout=10) as response:
            if response.geturl() != url:
                raise ValueError("Unexpected redirect")
            raw = response.read(1048577)
        if len(raw) > 1048576:
            raise ValueError("Oversized response")
        rows = json.loads(raw)["data"]
        if not isinstance(rows, list) or len(rows) > limit:
            raise ValueError("Invalid Twitch list")
        result = []
        for row in rows:
            login, name, title, count = (row[k] for k in ("user_login", "user_name", "title", "viewer_count"))
            if (not re.fullmatch(r"[a-z0-9_]{1,25}", login) or not isinstance(name, str)
                    or not isinstance(title, str) or type(count) is not int or count < 0):
                raise ValueError("Invalid Twitch stream data")
            result.append({"name": name, "title": title, "viewers": count, "url": "https://www.twitch.tv/" + login})
        return result
    except HTTPError as exc:
        raise ValueError(f"Twitch discovery returned HTTP {exc.code}. Check credentials or rate limits.") from None
    except (OSError, HTTPException, ValueError, TypeError, KeyError):
        raise ValueError("Twitch did not return a valid stream directory page.") from None


def register(subparsers):
    directory = subparsers.add_parser("discover-rooms", help="List Twitch streams for manual review")
    directory.add_argument("--game-id", required=True, help="Twitch game/category ID")
    directory.add_argument("--language", help="Two-letter language code or other")
    directory.add_argument("--limit", type=int, default=10, help="Return up to 100 live rooms")
    tango_server = subparsers.add_parser("tango-webhook", help="Receive authorized Tango follow and stream chat counts")
    tango_stats = subparsers.add_parser("tango-stats", help="Show counts of received Tango follows and stream chat")
    for parser in (tango_server, tango_stats):
        parser.add_argument("--creator", help="Tango encrypted creator ID (or TANGO_CREATOR_ID)")
    tango_server.add_argument("--port", type=int, default=8093)
    subparsers.add_parser("tiktok-stats", help="Read the authorized TikTok creator's aggregate profile statistics")
    run_server = subparsers.add_parser("fanvue-webhook", help="Receive signed creator follower events")
    stats = subparsers.add_parser("fanvue-stats", help="Count unique Fanvue follow events received")
    for parser in (run_server, stats):
        parser.add_argument("--creator", help="Authorized Fanvue creator UUID (or set FANVUE_CREATOR_UUID)")
    run_server.add_argument("--port", type=int, default=8092)


def run(args, db, rules=None):
    if args.command == "discover-rooms":
        print(json.dumps(discover_rooms(args.game_id, args.language, args.limit)))
        return 0
    if args.command.startswith("tango-"):
        creator = args.creator or os.getenv("TANGO_CREATOR_ID", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", creator):
            raise ValueError("Supply the authorized Tango creator ID through --creator or TANGO_CREATOR_ID.")
        db.execute(TANGO_TABLE)
        if args.command == "tango-stats":
            totals = {"follows": 0, "viewer_chat_messages": 0}
            totals.update(db.execute("SELECT kind,SUM(number) FROM tango_receipts WHERE creator=? GROUP BY kind",
                                     (_digest(creator),)).fetchall())
            print(json.dumps(totals))
            return 0
        secret = os.getenv("TANGO_WEBHOOK_SECRET", "")
        if not secret or not 1 <= args.port <= 65535:
            raise ValueError("Set TANGO_WEBHOOK_SECRET and use a port between 1 and 65535.")
        with HTTPServer(("127.0.0.1", args.port), _receiver(db, creator, secret.encode(), tango=True)) as server:
            print(f"Tango receiver listening at http://127.0.0.1:{args.port}/webhooks/tango", flush=True)
            server.serve_forever()
        return 0
    if args.command == "tiktok-stats":
        print(json.dumps(tiktok_stats()))
        return 0
    try:
        creator = _uuid(args.creator or os.getenv("FANVUE_CREATOR_UUID", ""))
    except ValueError:
        raise ValueError("Set FANVUE_CREATOR_UUID or supply --creator with the authorized creator UUID.") from None
    db.execute(TABLE)
    if args.command == "fanvue-stats":
        total = db.execute("SELECT COUNT(*) FROM fanvue_follow_receipts WHERE creator=?", (_digest(creator),)).fetchone()[0]
        print(json.dumps({"unique_follow_events_received": total}))
        return 0
    if args.command != "fanvue-webhook":
        raise ValueError("Unknown creator connector command")
    secret = os.getenv("FANVUE_WEBHOOK_SECRET", "")
    if not secret:
        raise ValueError("Set FANVUE_WEBHOOK_SECRET to the signing secret for this destination.")
    if not 1 <= args.port <= 65535:
        raise ValueError("Choose a TCP port between 1 and 65535.")
    with HTTPServer(("127.0.0.1", args.port), _receiver(db, creator, secret.encode())) as server:
        print(f"Fanvue receiver listening at http://127.0.0.1:{args.port}{PATH}", flush=True)
        server.serve_forever()
    return 0
