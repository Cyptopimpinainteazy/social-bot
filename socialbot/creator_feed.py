"""Read a creator's Social Stream Ninja public-room JSON export or local POST."""
import hashlib
import json
import os
import secrets
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import engine

COMMANDS = ("creator-import", "creator-listen")
SOURCES = ("bigo", "tiktok", "stripchat", "whatnot", "cam4", "camsoda", "bongacams",
           "fansly", "myfreecams", "livejasmin")
MAX_FILE = 32 * 1024 * 1024
MAX_POST = 256 * 1024


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif not self.hidden and tag in ("p", "br", "div"):
            self.parts.append(" ")
        elif not self.hidden and tag == "img":
            self.parts.append(dict(attrs).get("alt") or "")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _text(value, literal):
    if not isinstance(value, str):
        return ""
    if not literal:
        parsed = _PlainText()
        parsed.feed(value)
        parsed.close()
        value = "".join(parsed.parts)
    return " ".join(value.split())


def _opaque(value):
    if isinstance(value, str) and value.strip():
        return value
    if type(value) is int and value >= 0:
        return str(value)
    return None


def normalize(row, platform, channel, source_name=None):
    if not isinstance(row, dict) or row.get("type") != platform:
        return None
    if source_name is not None and row.get("sourceName") != source_name:
        return None
    if any(row.get(k) for k in ("private", "isPrivate", "isWhisper", "whisper", "privateMessage",
                                "isPrivateMessage", "isDM", "delete", "action", "membership")):
        return None
    if row.get("event") not in ("", None, False) or row.get("hasDonation"):
        return None
    author = _text(row.get("chatname"), row.get("textonly") is True)
    text = _text(row.get("chatmessage"), row.get("textonly") is True)
    if not author or not text:
        return None
    meta = row.get("meta")
    native = _opaque(meta.get("messageId")) if isinstance(meta, dict) else None
    row_id, stamp = _opaque(row.get("id")), row.get("timestamp")
    if native is not None:
        identity = ["native", native]
    elif row_id is not None and type(stamp) is int and stamp > 0:
        # The history/export ID belongs to this installation. The captured
        # millisecond timestamp also scopes IDs that got reused after a reset.
        identity = ["ssn", row_id, stamp]
    else:
        return None
    eid = "ssn:" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    result = {"platform": platform, "channel": channel, "kind": "chat", "id": eid,
              "user": author, "text": text}
    uid = _opaque(row.get("userid"))
    if uid:
        result["user_id"] = uid
    return result


def _record(db, row, rules, platform, channel, source_name):
    event = normalize(row, platform, channel, source_name)
    if not event:
        return "ignored"
    inserted, _ = engine.record(db, event, rules, origin="creator-feed")
    return "added" if inserted else "duplicate"


def import_file(db, path, rules, platform, channel, source_name=None):
    with Path(path).open("rb") as f:
        raw = f.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE:
        raise ValueError("Choose a shorter history export (maximum 32 MiB).")
    try:
        rows = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Invalid SSN history JSON.") from None
    if not isinstance(rows, list):
        raise ValueError("Choose the JSON array from the SSN Chat History export.")
    counts = {"added": 0, "duplicate": 0, "ignored": 0, "invalid": 0}
    # SSN's history export is newest first. Preserve chat order for the inbox.
    for row in reversed(rows):
        try:
            counts[_record(db, row, rules, platform, channel, source_name)] += 1
        except (ValueError, RecursionError):
            counts["invalid"] += 1
    return counts


def _handler(db, rules, platform, channel, source_name, token, origin):
    class Receiver(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            # The route contains the secret. Never log the query, path or body.
            pass

        def _authorized(self):
            expected_host = ("127.0.0.1:" + str(self.server.server_port), "localhost:" + str(self.server.server_port))
            return (self.headers.get("Host") in expected_host and
                    secrets.compare_digest(self.path.encode(), ("/ssn/" + token).encode()) and
                    self.headers.get("Origin") in (None, origin))

        def _reply(self, status):
            self.send_response(status)
            self.send_header("Cache-Control", "no-store")
            if self.headers.get("Origin") == origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Vary", "Origin")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_OPTIONS(self):
            self._reply(204 if self._authorized() else 403)

        def do_POST(self):
            if not self._authorized():
                return self._reply(403)
            if self.headers.get_content_type() != "application/json":
                return self._reply(415)
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._reply(400)
            if not 0 < size <= MAX_POST or self.headers.get("Transfer-Encoding"):
                return self._reply(413)
            try:
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    return self._reply(400)
                _record(db, data, rules, platform, channel, source_name)
            except (ValueError, RecursionError, TimeoutError):
                return self._reply(400)
            self._reply(200)
    return Receiver


def register(cmd):
    for name in COMMANDS:
        p = cmd.add_parser(name, help=("Import an SSN Chat History JSON file" if name == "creator-import"
                                       else "Receive creator-selected SSN chat messages on loopback"))
        p.add_argument("platform", choices=SOURCES)
        p.add_argument("--channel", required=True, help="Local label for this single creator room")
        p.add_argument("--source-name", help="Require the exact optional SSN sourceName field")
        if name == "creator-import":
            p.add_argument("file", type=Path)
        else:
            p.add_argument("--port", type=int, default=8766)
            p.add_argument("--origin", default="https://socialstream.ninja", help="Exact SSN dock origin for browser POSTs")


def run(args, db, rules):
    channel = engine.field(args.channel, "channel", 128).casefold()
    if args.command == "creator-import":
        print(json.dumps(import_file(db, args.file, rules, args.platform, channel, args.source_name)))
        return 0
    if not 1 <= args.port <= 65535:
        raise ValueError("Choose a TCP port from 1 to 65535.")
    origin = urlsplit(args.origin)
    if (origin.scheme not in ("http", "https") or not origin.netloc or origin.path or origin.query
            or origin.fragment or origin.username is not None or not args.origin.isascii()
            or any(ord(c) <= 32 for c in args.origin)):
        raise ValueError("Set --origin to the SSN dock's exact HTTP(S) origin.")
    token = os.getenv("SOCIALBOT_SSN_TOKEN") or secrets.token_urlsafe(32)
    if len(token) < 24 or not token.isascii() or not all(c.isalnum() or c in "-_" for c in token):
        raise ValueError("Set SOCIALBOT_SSN_TOKEN to at least 24 URL-safe characters.")
    handler = _handler(db, rules, args.platform, channel, args.source_name, token, args.origin)
    with HTTPServer(("127.0.0.1", args.port), handler) as srv:
        print(f"Private destination for the local SSN dock: http://127.0.0.1:{args.port}/ssn/{token}", flush=True)
        srv.serve_forever()
    return 0
