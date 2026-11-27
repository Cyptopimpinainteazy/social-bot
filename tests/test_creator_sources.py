import argparse
from contextlib import redirect_stdout
import hashlib
import hmac
from http.client import HTTPConnection
from http.server import HTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from socialbot import creator_sources as sources

OWNER = "00000000-0000-4000-a000-000000000001"
FAN = "00000000-0000-4000-a000-000000000002"
EVENT = "00000000-0000-4000-a000-000000000003"
SECRET = b"sample-only-in-test"


def payload(owner=OWNER, follower=FAN, event=EVENT):
    return json.dumps({"id": event, "type": "creator.follow.created", "data": {
        "object": "follow", "creator": {"uuid": owner}, "follower": {"uuid": follower, "handle": "private-name"},
        "tracking": {"link_url": "https://example.org/private-link"}}}, indent=2).encode()


def header(body, time_value=1000):
    return f"t={time_value},v0=" + hmac.new(SECRET, str(time_value).encode() + b"." + body, hashlib.sha256).hexdigest()


class Fanvue(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute(sources.TABLE)

    def submit(self, body, signature=None):
        return sources.accept_fanvue(self.db, OWNER, body, header(body) if signature is None else signature, SECRET, now=1000)

    def test_native_event_retry_survives_reopen_without_viewer_data(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "stats.db"
            for expected in ["received", "duplicate"]:
                with sqlite3.connect(path) as db:
                    db.execute(sources.TABLE)
                    body = payload()
                    self.assertEqual(sources.accept_fanvue(db, OWNER, body, header(body), SECRET, now=1000), expected)
                    self.assertEqual(db.execute("select count(*) from fanvue_follow_receipts").fetchone()[0], 1)
                db.close()
            data = path.read_bytes()
            for private in (OWNER, EVENT, FAN, "private-name", "private-link"):
                self.assertNotIn(private.encode(), data)

    def test_rotation_and_tampering(self):
        body = payload()
        timestamp, valid_mac = header(body).split(",v0=")
        self.assertEqual(self.submit(body, f"{timestamp},v0={'0'*64},{valid_mac}"), "received")
        for invalid in [header(body + b" "), header(body, 699), header(body, 1301),
                        f"t=1000,t=1000,v0={valid_mac}", "t=1000,v0=bad", ""]:
            with self.subTest(header=invalid), self.assertRaises(sources.InvalidSignature):
                self.submit(body, invalid)

    def test_signed_ignored_tests_and_validation_probe(self):
        self.assertEqual(self.submit(payload(owner=FAN)), "ignored")
        self.assertEqual(self.submit(payload(follower="TEST-" + FAN)), "test")
        self.assertEqual(self.submit(b'{"type":"creator.message.received","data":{"text":"private"}}'), "ignored")
        self.assertEqual(self.submit(b'{"type":"fanvue.webhook.verification","timestamp":"2026-06-09T08:39:33.139Z"}', ""), "probe")
        self.assertEqual(self.db.execute("select count(*) from fanvue_follow_receipts").fetchone()[0], 0)
        with self.assertRaises(ValueError):
            self.submit(b'{"type":"creator.follow.created"}')

    def test_loopback_post_and_stats(self):
        parser = argparse.ArgumentParser()
        sources.register(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["fanvue-stats", "--creator", OWNER])
        with HTTPServer(("127.0.0.1", 0), sources._receiver(self.db, OWNER, SECRET)) as server:
            statuses = []
            body = payload()
            def client():
                conn = HTTPConnection(*server.server_address, timeout=3)
                try:
                    conn.request("POST", sources.PATH, body, {"X-Fanvue-Signature": header(body, int(time.time()))})
                    response = conn.getresponse()
                    response.read()
                    statuses.append(response.status)
                finally:
                    conn.close()
            t = threading.Thread(target=client)
            t.start()
            stream = io.StringIO()
            with redirect_stdout(stream):
                server.handle_request()
                sources.run(args, self.db)
            t.join(3)
            self.assertEqual(statuses, [204])
            self.assertEqual(stream.getvalue().splitlines(), ["Fanvue follow received", '{"unique_follow_events_received": 1}'])


class TikTok(unittest.TestCase):
    @patch.dict(os.environ, {"TIKTOK_ACCESS_TOKEN": "testtoken"})
    @patch.object(sources, "urlopen")
    def test_minimum_fields_and_clean_output(self, fetch):
        numbers = dict(zip(sources.TIKTOK_FIELDS, [0, 17, 1200, 9]))
        response = io.BytesIO(json.dumps({"data": {"user": dict(numbers, display_name="private")}, "error": {"code": "ok"}}).encode())
        response.geturl = lambda: fetch.call_args.args[0].full_url
        fetch.return_value = response
        self.assertEqual(sources.tiktok_stats(), numbers)
        req = fetch.call_args.args[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.get_header("Authorization"), "Bearer testtoken")
        self.assertEqual(req.full_url, "https://open.tiktokapis.com/v2/user/info/?fields=follower_count,following_count,likes_count,video_count")

    @patch.dict(os.environ, {"TIKTOK_ACCESS_TOKEN": "testtoken"})
    @patch.object(sources, "urlopen")
    def test_missing_metrics_are_errors(self, fetch):
        resp = io.BytesIO(b'{"data":{"user":{}},"error":{"code":"ok"}}')
        resp.geturl = lambda: fetch.call_args.args[0].full_url
        fetch.return_value = resp
        with self.assertRaisesRegex(ValueError, "four authorized"):
            sources.tiktok_stats()


class NativeShortcut(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for the native JS fixture")
    def test_only_the_requesting_viewer_receives_a_notice(self):
        javascript = Path(__file__).resolve().parents[1] / "integrations" / "chaturbate-v2" / "room-info.js"
        harness = """
const vm = require('node:vm');
const fs = require('node:fs');
const code = fs.readFileSync(process.argv[1], 'utf8');
const calls = [];
for (const [shortcut, name] of [['room-info','viewer'],['other','viewer'],['room-info','']]) {
    vm.runInNewContext(code, {$shortcut: {name:shortcut}, $user:{username:name},
        $room:{sendNotice:(...args)=>calls.push(args)}});
}
process.stdout.write(JSON.stringify(calls));
"""
        calls = json.loads(subprocess.check_output(["node", "-e", harness, str(javascript)], text=True))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], {"toUsername": "viewer"})


    @unittest.skipUnless(shutil.which("node"), "Node is required for native JS fixtures")
    def test_native_greeting_regulars_tip_privacy_and_independent_cooldowns(self):
        scripts = Path(__file__).resolve().parents[1] / "integrations" / "chaturbate-v2"
        harness = """
const vm = require('node:vm'), fs = require('node:fs'), path = require('node:path');
const events = [], kv = new Map();
let clock = 1000;
const env = {
    $kv: {get:k=>kv.get(k),set:(k,v)=>kv.set(k,v)},
    $room: {sendNotice:(...args)=>events.push(args)}, Date: {now:()=>clock},
    $settings: {greetingText:'Welcome!',regularUsernames:' first,ALICE ',
                regularGreeting:'Welcome back!',greetingCooldownSeconds:10,
                tipThanksText:'Thank you!',tipCooldownSeconds:15}
};
function run(file, extras={}) {
    vm.runInNewContext(fs.readFileSync(path.join(process.argv[1],file+'.js'),'utf8'), {...env,...extras});
}
run('app-start');
run('user-enter',{$user:{username:'alice'}});
run('user-enter',{$user:{username:'Bob'}});
run('tip-received'); run('tip-received');
clock += 10000;
run('user-enter',{$user:{username:'Bob'}});
clock += 5000;
run('tip-received');
clock += 50000;
env.$settings.greetingText = ' '; env.$settings.tipThanksText = '';
run('tip-received'); run('user-enter',{$user:{username:'alice'}});
console.log(JSON.stringify({events,stored:[...kv]}));
"""
        result = json.loads(subprocess.check_output(["node", "-e", harness, str(scripts)], text=True))
        self.assertEqual(result["events"], [
            ["Welcome back!", {"toUsername": "alice"}], ["Thank you!"],
            ["Welcome!", {"toUsername": "Bob"}], ["Thank you!"],
        ])
        self.assertEqual(dict(result["stored"]), {"socialbot_entry_notice_at": 11000, "socialbot_tip_notice_at": 16000})


class Tango(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute(sources.TANGO_TABLE)

    def test_authorized_batch_count_privacy_retry_and_system_message(self):
        evt = {"id": "sc_123", "type": "stream.chat.messages.received", "creatorId": "mycreator",
               "data": {"streamId": "stream-id", "messages": [
                   {"systemMessage": False, "text": "sensitive phrase", "sender": {"id": "personal"}},
                   {"systemMessage": True, "text": "system"}, {"systemMessage": False, "text": "hello", "sender": {"id": "viewer2"}}]}}
        def submit(event, signed=True):
            body = json.dumps(event).encode()
            sig = header(body).replace("v0=", "v1=") if signed else header(body)
            return sources.accept_tango(self.db, "mycreator", body, sig, SECRET, now=1000)
        self.assertEqual(submit(evt), "viewer_chat_messages")
        self.assertEqual(submit(evt), "duplicate")
        self.assertEqual(submit(dict(evt, creatorId="wrong")), "ignored")
        self.assertEqual(submit(dict(evt, type="chat.message.received")), "ignored")
        self.assertEqual(submit(dict(evt, id="follow_new", type="follower.added", data={"follower": {"id": "person"}})), "follows")
        with self.assertRaises(sources.InvalidSignature):
            submit(evt, False)
        rows = self.db.execute("select kind,number from tango_receipts order by kind").fetchall()
        self.assertEqual(rows, [("follows", 1), ("viewer_chat_messages", 2)])
        for token in ["personal", "sensitive phrase", "sc_123", "mycreator"]:
            self.assertNotIn(token, "\n".join(self.db.iterdump()))

    def test_options_probe_route_then_authenticated_post(self):
        with HTTPServer(("127.0.0.1", 0), sources._receiver(self.db, "modelX", SECRET, tango=True)) as server:
            def visit(method, path, expected, data=None, sig=None):
                statuses = []
                def send():
                    conn = HTTPConnection(*server.server_address, timeout=3)
                    try:
                        conn.request(method, path, body=data, headers={"TNG-Webhook-Signature": sig} if sig else {})
                        resp = conn.getresponse()
                        resp.read()
                        statuses.append(resp.status)
                    finally:
                        conn.close()
                t = threading.Thread(target=send)
                t.start()
                with redirect_stdout(io.StringIO()):
                    server.handle_request()
                t.join(3)
                self.assertEqual(statuses, [expected])
            visit("OPTIONS", "/webhooks/tango", 200)
            visit("OPTIONS", "/webhooks/tango/modelX", 404)
            visit("POST", "/webhooks/tango", 404, b"{}")
            visit("POST", "/webhooks/tango/modelX", 401, b"{}")
            body = b'{"id":"follow_x","creatorId":"modelX","type":"follower.added","data":{"follower":{"id":"opaque"}}}'
            visit("POST", "/webhooks/tango/modelX", 204, body, header(body, int(time.time())).replace("v0=", "v1="))


class Discovery(unittest.TestCase):
    @patch.dict(os.environ, {"TWITCH_CLIENT_ID": "test-id", "TWITCH_DISCOVERY_TOKEN": "test-token"})
    @patch.object(sources, "urlopen")
    def test_single_read_only_directory_request(self, fetch):
        body = {"data": [{"user_login": "stage", "user_name": "Stage", "viewer_count": 5,
                          "title": "Live Q&A", "unrelated_field": "private"}]}
        res = io.BytesIO(json.dumps(body).encode())
        res.geturl = lambda: fetch.call_args.args[0].full_url
        fetch.return_value = res
        self.assertEqual(sources.discover_rooms("509658", "en", 4), [
            {"name": "Stage", "title": "Live Q&A", "viewers": 5, "url": "https://www.twitch.tv/stage"}])
        request = fetch.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.twitch.tv/helix/streams?game_id=509658&first=4&language=en")
        self.assertEqual(request.get_method(), "GET")
        fetch.assert_called_once()

    @patch.object(sources, "urlopen")
    def test_bad_filters_fail_before_request(self, fetch):
        for game, language, limit in [("non-numeric", "en", 5), ("1", "overlong", 10), ("1", "en", 0)]:
            with self.assertRaises(ValueError):
                sources.discover_rooms(game, language, limit)
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
