"""Creator dashboard rendering, data boundaries and local HTTP behavior."""

import http.client
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer
from pathlib import Path

from socialbot import dashboard


class TableCells(HTMLParser):
    def __init__(self):
        super().__init__()
        self.table_rows = []
        self.cells = None
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.cells = []
        elif tag in {"td", "th"}:
            self.current = []

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"}:
            self.cells.append("".join(self.current))
            self.current = None
        elif tag == "tr":
            self.table_rows.append(self.cells)
            self.cells = None


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "social #1?.db"
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE events(platform, channel, event_id, kind, viewer, text, received_at);
                CREATE TABLE drafts(id INTEGER PRIMARY KEY, platform, channel, event_id, body, status, reason, created_at);
                CREATE TABLE viewers(platform, channel, viewer, joins, chats, gifts, follows, last_seen);
            """)

    def test_counts_each_channel_once_and_escapes_untrusted_text(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                INSERT INTO events VALUES ('twitch','music','1','chat','<Alice>','<script>one</script>',1),
                                          ('twitch','music','2','gift','Bob','Nice',2),
                                          ('twitch','art','1','chat','Bob','Paint',3);
                INSERT INTO viewers VALUES ('twitch','music','<Alice>',0,1,0,0,1),
                                           ('twitch','music','Bob',0,0,1,0,2),
                                           ('twitch','art','Bob',0,1,0,0,3);
                INSERT INTO drafts VALUES (1,'twitch','music','1','<button>','pending','A&B',1),
                                          (2,'twitch','music','2','Thank you','approved','',2);
            """)
        rendered = dashboard.render_dashboard(self.path)
        parser = TableCells()
        parser.feed(rendered)
        self.assertIn(["twitch", "2", "3", "1", "3", "2", "1"], parser.table_rows)
        self.assertIn(["twitch", "music", "2", "1", "2", "2", "1"], parser.table_rows)
        self.assertIn(["twitch", "art", "1", "0", "1", "0", "0"], parser.table_rows)
        for fragment in ["&lt;Alice&gt;", "&lt;script&gt;", "&lt;button&gt;", "A&amp;B"]:
            self.assertIn(fragment, rendered)
        self.assertNotIn("<script>", rendered)

    def test_empty_and_missing_databases(self):
        rendered = dashboard.render_dashboard(self.path)
        self.assertIn("No events received yet", rendered)
        self.assertIn("No reply drafts yet", rendered)
        missing = self.path.with_name("missing.db")
        with self.assertRaises(sqlite3.OperationalError):
            dashboard.render_dashboard(missing)
        self.assertFalse(missing.exists())

    def test_activity_shows_only_the_latest_twelve(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.executemany("INSERT INTO events VALUES ('demo','studio',?,'chat','viewer',?,?)",
                           ((str(i), f"event-number-{i}!", i) for i in range(15)))
            db.commit()
        rendered = dashboard.render_dashboard(self.path)
        self.assertIn("event-number-14!", rendered)
        self.assertIn("event-number-3!", rendered)
        self.assertNotIn("event-number-2!", rendered)

    def test_http_serves_snapshot_and_rejects_foreign_host(self):
        with ThreadingHTTPServer(("127.0.0.1", 0), dashboard._handler(self.path)) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)) as client:
                    client.request("GET", "/")
                    resp = client.getresponse()
                    self.assertEqual(200, resp.status)
                    self.assertEqual("no-store", resp.headers["Cache-Control"])
                    self.assertIn(b"Creator dashboard", resp.read())
                    client.request("HEAD", "/", headers={"Host": "unrelated.example"})
                    resp = client.getresponse()
                    self.assertEqual(403, resp.status)
                    self.assertEqual(b"", resp.read())
            finally:
                server.shutdown()
                worker.join(5)
