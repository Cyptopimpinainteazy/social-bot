"""Accept normalized events from one creator-controlled integration on loopback."""
import hashlib
import hmac
import http.client
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import engine

PATH = "/v1/events"
MAX_BYTES = 32768

def register(cmd):
    p = cmd.add_parser("bridge", help="Connect your own event producer to Social Bot")
    sub = p.add_subparsers(dest="bridge_action", required=True)
    serve = sub.add_parser("serve", help="Listen on loopback for one platform and room")
    serve.add_argument("--platform", required=True)
    serve.add_argument("--channel", required=True)
    submit = sub.add_parser("submit", help="Send a normalized event JSON file to a local receiver")
    submit.add_argument("file", type=Path)
    for parser in (serve, submit):
        parser.add_argument("--secret-env", default="SOCIALBOT_BRIDGE_SECRET")
        parser.add_argument("--port", type=int, default=8094)

def signature(secret, stamp, body):
    return hmac.new(secret, stamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()

def handler(db, rules, platform, channel, secret):
    class Receiver(BaseHTTPRequestHandler):
        timeout = 5
        def log_message(self, fmt, *args):
            pass

        def result(self, code, data):
            raw = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            if self.path != PATH:
                return self.result(404, {"error":"Unknown route"})
            for name in ("Content-Length", "X-SocialBot-Time", "X-SocialBot-Signature"):
                if len(self.headers.get_all(name, [])) != 1:
                    return self.result(400, {"error":"Invalid request headers"})
            size = self.headers["Content-Length"]
            stamp = self.headers["X-SocialBot-Time"]
            supplied = self.headers["X-SocialBot-Signature"]
            if self.headers.get("Transfer-Encoding") or not re.fullmatch(r"[1-9][0-9]{0,5}", size) or int(size)>MAX_BYTES:
                return self.result(413, {"error":"Invalid event size"})
            if not re.fullmatch(r"[0-9]{1,12}", stamp) or abs(time.time()-int(stamp))>300:
                return self.result(401, {"error":"Stale timestamp"})
            if not re.fullmatch(r"[0-9a-f]{64}", supplied):
                return self.result(401, {"error":"Invalid signature"})
            raw = self.rfile.read(int(size))
            if len(raw)!=int(size) or not hmac.compare_digest(signature(secret, stamp, raw), supplied):
                return self.result(401, {"error":"Invalid signature"})
            try:
                event = json.loads(raw)
                if not isinstance(event, dict) or event.get("platform") != platform or event.get("channel") != channel:
                    return self.result(403, {"error":"Unexpected source room"})
                accepted, draft = engine.record(db, event, rules(), origin="creator-bridge")
            except (ValueError, RecursionError):
                return self.result(422, {"error":"Invalid event"})
            self.result(201 if accepted else 200, {"added":accepted, "draft_id":draft})
    return Receiver

def run(args, db, rules):
    secret = os.getenv(args.secret_env, "").encode()
    if len(secret) < 32:
        raise ValueError("Set the bridge secret to at least 32 random bytes of text")
    if not 1 <= args.port <= 65535:
        raise ValueError("Choose a port from 1 through 65535")
    if args.bridge_action == "serve":
        platform = engine.platform_name(args.platform)
        channel = engine.field(args.channel, "channel", 128).casefold()
        with HTTPServer(("127.0.0.1", args.port), handler(db, rules, platform, channel, secret)) as server:
            print(f"Event bridge for {platform}/{channel} on 127.0.0.1:{args.port}{PATH}", flush=True)
            server.serve_forever()
    else:
        with args.file.open("rb") as source:
            raw = source.read(MAX_BYTES+1)
        if not 0 < len(raw) <= MAX_BYTES:
            raise ValueError("Invalid event size")
        stamp = str(int(time.time()))
        conn = http.client.HTTPConnection("127.0.0.1", args.port, timeout=7)
        try:
            conn.request("POST", PATH, raw, {"X-SocialBot-Signature":signature(secret, stamp, raw),
                         "X-SocialBot-Time":stamp, "Content-Type":"application/json"})
            reply = conn.getresponse()
            if reply.status not in (200,201):
                raise ValueError(f"Event bridge rejected submission (HTTP {reply.status})")
            print(reply.read(1024).decode())
        finally:
            conn.close()
    return 0
