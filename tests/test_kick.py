import base64
from contextlib import redirect_stdout
from datetime import datetime, timezone
from http.client import HTTPConnection
from http.server import HTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

if importlib.util.find_spec("cryptography") is None:
    raise unittest.SkipTest("Install the kick extra to run the Kick signature and webhook tests.")

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from socialbot import kick


CHANNEL = "stage"
BROADCASTER = {"user_id": 12345, "channel_slug": CHANNEL}
VIEWER = {"user_id": 71, "username": "Fan"}
CHAT = {"broadcaster": BROADCASTER, "sender": VIEWER, "content": "hello!", "message_id": "native-chat-id"}
ID = "01KCTMJ2X3DXT814CVX8FR9T1D"


class SignedEvents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.pub = self.private.public_key()
        patcher = patch.object(kick, "_fetch_public_key", return_value=self.pub)
        self.fetch = patcher.start()
        self.addCleanup(patcher.stop)
        self.keys = kick.KeyCache()

    def sign(self, data=CHAT, kind="chat.message.sent", age=0):
        # Spaces and line breaks exercise verification over exact bytes.
        body = json.dumps(data, indent=2).encode()
        timestamp = datetime.fromtimestamp(time.time() - age, timezone.utc).isoformat()
        signed = b".".join((ID.encode(), timestamp.encode(), body))
        sig = self.private.sign(signed, padding.PKCS1v15(), hashes.SHA256())
        headers = {"Kick-Event-Message-Id": ID, "Kick-Event-Message-Timestamp": timestamp,
                   "Kick-Event-Signature": base64.b64encode(sig).decode(),
                   "Kick-Event-Type": kind, "Kick-Event-Version": "1"}
        return headers, body

    def accept(self, data=CHAT, kind="chat.message.sent", age=0):
        return kick.verify_event(*self.sign(data, kind, age), CHANNEL, "12345", self.keys)

    def test_named_events(self):
        self.assertEqual(self.accept(), {"id": "native-chat-id", "platform": "kick", "channel": CHANNEL,
                                        "kind": "chat", "user": "Fan", "text": "hello!", "user_id": "71"})
        follow = self.accept({"broadcaster": BROADCASTER, "follower": VIEWER}, "channel.followed")
        self.assertEqual((follow["id"], follow["kind"]), (ID, "follow"))
        gift = self.accept({"broadcaster": BROADCASTER, "gifter": VIEWER, "giftees": [VIEWER]},
                           "channel.subscription.gifts")
        self.assertEqual((gift["kind"], gift["text"]), ("gift", "1 gift subscription(s)"))
        gift = self.accept({"broadcaster": BROADCASTER, "sender": VIEWER, "gift": {"amount": 5, "name": "Wave"}},
                           "kicks.gifted")
        self.assertEqual(gift["text"], "Wave (5 KICKs)")
        self.fetch.assert_called_once()

    def test_freshness_binding_and_shape(self):
        for age in [-400, 400]:
            with self.subTest(age=age), self.assertRaises(kick.KickError):
                self.accept(age=age)
        for broadcaster in [dict(BROADCASTER, user_id=9), dict(BROADCASTER, channel_slug="other")]:
            with self.subTest(broadcaster=broadcaster), self.assertRaises(kick.KickError):
                self.accept(dict(CHAT, broadcaster=broadcaster))
        self.assertIsNone(self.accept(dict(CHAT, sender=dict(VIEWER, user_id=12345))))
        self.assertIsNone(self.accept(dict(CHAT, sender={"is_anonymous": True})))
        with self.assertRaises(kick.KickError):
            self.accept(CHAT, "kicks.gifted")
        self.assertIsNone(self.accept(CHAT, "livestream.status.updated"))

    def test_signature_covers_exact_request_and_refresh_is_bounded(self):
        headers, body = self.sign()
        for alternate in [body + b" ", json.dumps(CHAT).encode()]:
            with self.assertRaises(kick.KickError):
                kick.verify_event(headers, alternate, CHANNEL, "12345", self.keys)
        for label, value in [("Kick-Event-Message-Id", "01KCTMJ2X3DXT814CVX8FR9T1E"),
                             ("Kick-Event-Signature", "invalid!")]:
            with self.subTest(label=label), self.assertRaises(kick.KickError):
                kick.verify_event({**headers, label: value}, body, CHANNEL, "12345", self.keys)
        self.fetch.assert_called_once()

    def test_rotated_key_refetch(self):
        old = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
        self.fetch.side_effect = [old, self.pub]
        with patch.object(kick.time, "monotonic", return_value=10):
            self.keys.get()
        with patch.object(kick.time, "monotonic", return_value=80):
            self.assertEqual(self.accept()["id"], "native-chat-id")
            self.assertEqual(self.fetch.call_count, 2)

    def test_http_entrypoint(self):
        consumed = []
        with HTTPServer(("127.0.0.1", 0), kick._handler(CHANNEL, "12345", self.keys, consumed.append)) as server:
            def post(path, body, headers):
                io_thread = threading.Thread(target=server.handle_request)
                io_thread.start()
                conn = HTTPConnection(*server.server_address, timeout=3)
                try:
                    conn.request("POST", path, body=body, headers=headers)
                    result = conn.getresponse()
                    result.read()
                    return result.status
                finally:
                    conn.close()
                    io_thread.join(3)
            headers, body = self.sign()
            self.assertEqual(post("/other", body, headers), 404)
            self.assertEqual(post(kick.PATH, body, {}), 400)
            self.assertEqual(post(kick.PATH, body, {**headers, "Content-Length": "65537"}), 413)
            self.assertEqual(post(kick.PATH, body + b" ", headers), 400)
            self.assertFalse(consumed)
            self.assertEqual(post(kick.PATH, body, headers), 204)
            self.assertEqual([e["id"] for e in consumed], ["native-chat-id"])


class TrustedKey(unittest.TestCase):
    def test_current_endpoint_json_envelope(self):
        pub = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
        pem = pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        response = io.BytesIO(json.dumps({"data": {"public_key": pem.decode()}}).encode())
        response.geturl = lambda: kick.PUBLIC_KEY_URL
        with patch.object(kick, "urlopen", return_value=response) as opened:
            actual = kick._fetch_public_key()
        self.assertEqual(actual.public_numbers(), pub.public_numbers())
        self.assertEqual(opened.call_args.args[0].full_url, "https://api.kick.com/public/v1/public-key")

    def test_key_fetch_failure_is_throttled(self):
        with patch.object(kick, "_fetch_public_key", side_effect=kick.KickKeyUnavailable) as fetch:
            cache = kick.KeyCache()
            for i in range(3):
                with self.assertRaises(kick.KickKeyUnavailable):
                    cache.get()
            fetch.assert_called_once()

    def test_configuration_required_and_loopback_specific(self):
        with patch.dict(os.environ, {"KICK_BROADCASTER_ID": ""}), self.assertRaises(kick.KickError):
            kick.serve_kick("stage", list.append)
        with (patch.dict(os.environ, {"KICK_BROADCASTER_ID": "12345"}),
              patch.object(kick.KeyCache, "get"), patch.object(kick, "HTTPServer") as server):
            kick.serve_kick("stage", list.append)
        self.assertEqual(server.call_args.args[0], ("127.0.0.1", 8091))


class PrivateConsole(unittest.TestCase):
    def test_only_operator_text_and_counts_appear_no_disk_writes(self):
        rules = {"keywords": [(re.compile("hello"), "Hi @{user}!")], "follow": "Thank you, {user}!", "gift": "Thanks!"}
        event = {"id": "secret-message-id", "kind": "chat", "user_id": "private-identity",
                 "user": "private-name", "text": "hello my secret-chat \x1b[31m"}
        def fixture(channel, consume, port):
            self.assertEqual((channel, port), ("stage", 9999))
            consume(event)
            consume(dict(event))
            consume(dict(event, id="another-secret", kind="follow"))
        with tempfile.TemporaryDirectory() as folder, patch.object(kick, "serve_kick", side_effect=fixture):
            cwd = os.getcwd()
            try:
                os.chdir(folder)
                out = io.StringIO()
                with redirect_stdout(out), patch("sqlite3.connect", side_effect=AssertionError("unexpected database")):
                    kick.listen_kick("stage", rules, 9999)
                self.assertEqual(list(Path(folder).iterdir()), [])
            finally:
                os.chdir(cwd)
        self.assertEqual(out.getvalue().splitlines(), [
            "Kick chat · 1 this session  |  Suggested reply: Hi friend!",
            "Kick follow · 1 this session  |  Suggested reply: Thank you, friend!",
        ])

    def test_digest_cache_expires_and_remains_bounded(self):
        rules = {"keywords": []}
        def fixture(channel, consume, port):
            event = {"id": "sensitive-first", "kind": "chat", "text": "private"}
            with patch.object(kick.time, "monotonic", return_value=0):
                consume(event)
                consume(event)
                for i in range(4096):
                    consume(dict(event, id=f"message-{i}"))
                consume(event)
            with patch.object(kick.time, "monotonic", return_value=301):
                consume(event)
        out = io.StringIO()
        with redirect_stdout(out), patch.object(kick, "serve_kick", side_effect=fixture):
            kick.listen_kick("stage", rules)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 4099)
        self.assertEqual(lines[-1], "Kick chat · 4099 this session")
        self.assertNotIn("private", out.getvalue())


if __name__ == "__main__":
    unittest.main()
