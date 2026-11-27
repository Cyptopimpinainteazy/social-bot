import http.client
import json
import tempfile
import unittest
from pathlib import Path
from http.server import HTTPServer
from threading import Thread

from socialbot import creator_feed as feed
from socialbot import engine


def row(**changes):
    return {"type": "bigo", "sourceName": "Room A", "id": 7, "timestamp": 1700000000100,
            "chatname": "River", "userid": "42", "chatmessage": "<b>Hello</b> <img alt='there'>",
            **changes}


class CreatorFeedTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)
        self.rules = engine.load_rules()
        self.db = engine.connect(self.path / "rooms.db")
        self.addCleanup(self.db.close)

    def test_exact_source_public_room_text_and_export_keys(self):
        for source in feed.SOURCES:
            r = feed.normalize(row(type=source), source, "studio", "Room A")
            self.assertEqual((r["platform"], r["kind"], r["user_id"], r["text"]),
                             (source, "chat", "42", "Hello there"))
        for changes in [{"type": "other"}, {"sourceName":"Room B"}, {"hasDonation":"10 tokens"},
                        {"private": True}, {"event": "private_message"}, {"membership":"MEMBER"},
                        {"event": "viewer_update"}, {"id": None}, {"timestamp": None}]:
            with self.subTest(changes=changes):
                self.assertIsNone(feed.normalize(row(**changes), "bigo", "studio", "Room A"))
        literal = feed.normalize(row(textonly=True, chatmessage="2 < 3, use <b> tags"), "bigo", "studio")
        self.assertEqual(literal["text"], "2 < 3, use <b> tags")

    def test_native_or_local_ids_survive_retry_and_distinct_identical_chats_remain(self):
        native = row(meta={"messageId": "msg7"}, id=None)
        native_again = dict(native, timestamp=1710000000000, id=2222)
        extract = lambda r: feed.normalize(r, "bigo", "studio")["id"]
        self.assertEqual(extract(native), extract(native_again))
        self.assertNotEqual(extract(row()), extract(row(id=8)))
        self.assertNotEqual(extract(row()), extract(row(timestamp=1800000000000)))

    def test_history_read_twice_generates_one_set_of_drafts_and_cannot_grant_consent(self):
        file = self.path / "chat.json"
        file.write_text(json.dumps([row(id=8, chatmessage="!optin"), row()]), encoding="utf-8")
        first = feed.import_file(self.db, file, self.rules, "bigo", "studio")
        second = feed.import_file(self.db, file, self.rules, "bigo", "studio")
        self.assertEqual(first["added"], 2)
        self.assertEqual(second["duplicate"], 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 1)
        self.assertEqual([tuple(r) for r in self.db.execute("SELECT origin,text FROM events ORDER BY rowid")],
                         [("creator-feed", "Hello there"), ("creator-feed", "!optin")])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM community_members").fetchone()[0], 0)
        file.write_text('{"settings":"this is not a chat export"}')
        with self.assertRaises(ValueError):
            feed.import_file(self.db, file, self.rules, "bigo", "studio")

    def test_loopback_generic_post_acknowledgement_secret_origin_and_db_replay(self):
        secret = "x" * 32
        origin = "https://socialstream.ninja"
        # The production receiver uses the main thread. Create a same-thread
        # SQLite handle inside the test server thread for equivalent ownership.
        server = HTTPServer(("127.0.0.1", 0), None)
        def serve():
            db = engine.connect(self.path / "rooms.db")
            server.RequestHandlerClass = feed._handler(db, self.rules, "bigo", "studio", None, secret, origin)
            try:
                server.serve_forever(poll_interval=0.05)
            finally:
                db.close()
        thread = Thread(target=serve)
        thread.start()
        try:
            def request(path, method="POST", payload=None, browser_origin=origin):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
                try:
                    conn.request(method, path, body=json.dumps(payload or row()),
                                 headers={"Origin": browser_origin, "Content-Type":"application/json"})
                    response = conn.getresponse()
                    response.read()
                    return response.status
                finally:
                    conn.close()
            url = "/ssn/" + secret
            self.assertEqual(request(url, "OPTIONS"), 204)
            self.assertEqual(request("/ssn/incorrect"), 403)
            self.assertEqual(request(url, browser_origin="https://other.example"), 403)
            self.assertEqual(request(url, payload=[row()]), 400)
            self.assertEqual(request(url), 200)
            self.assertEqual(request(url), 200)
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        finally:
            server.shutdown()
            thread.join(timeout=4)
            server.server_close()
