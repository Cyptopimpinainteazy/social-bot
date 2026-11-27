"""End-to-end coverage for event replay, draft review and delivery boundaries."""
import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from socialbot import __main__ as cli
from socialbot import cohost, engine, twitch


def chat(identifier, **changes):
    return {"platform": "twitch", "channel": "studio", "id": identifier, "user": "River",
            "user_id": "123", "kind": "chat", "text": "Hello!", **changes}


class CoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "data.sqlite3"
        self.rules = engine.load_rules()

    def command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--db", str(self.path), *args])
        return code, out.getvalue(), err.getvalue()

    def approved(self, db, identifier, now=1000, **changes):
        _, draft_id = engine.record(db, chat(identifier, **changes), self.rules, origin="twitch-live", now=now)
        engine.review(db, draft_id, "approve")
        return draft_id

    def test_restart_duplicate_does_not_recount_but_distinct_rooms_and_platforms_do(self):
        with closing(engine.connect(self.path)) as db:
            self.assertTrue(engine.record(db, chat("e1"), self.rules)[0])
        with closing(engine.connect(self.path)) as db:
            self.assertEqual(engine.record(db, chat("e1", channel="Studio"), self.rules), (False, None))
            for changes in [{"channel": "studio2"}, {"platform": "bigo"}]:
                self.assertTrue(engine.record(db, chat("e1", **changes), self.rules)[0])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 3)
            self.assertEqual([r[0] for r in db.execute("SELECT chats FROM viewers")], [1, 1, 1])

    def test_keyword_boundaries_and_viewer_counters_with_rename(self):
        with closing(engine.connect(self.path)) as db:
            for item in [chat("entry", kind="join"), chat("word", text="this schedulebook"),
                         chat("match", text="When is the NEXT STREAM?"),
                         chat("gift", kind="gift"), chat("sub", kind="follow"),
                         chat("again", kind="join", user="RiverRenamed")]:
                engine.record(db, item, self.rules)
            reasons = {r["event_id"]: r["reason"] for r in db.execute("SELECT event_id,reason FROM drafts")}
            self.assertEqual(reasons, {"entry": "first_join", "match": "keyword", "gift": "gift",
                                       "sub": "follow", "again": "return_join"})
            row = db.execute("SELECT viewer,joins,chats,gifts,follows FROM viewers").fetchone()
            self.assertEqual(tuple(row), ("RiverRenamed", 2, 2, 1, 1))

    def test_demo_repeat_and_import_cannot_forge_live_delivery(self):
        self.assertEqual(self.command("demo")[0], 0)
        code, out, _ = self.command("demo")
        self.assertEqual(code, 0)
        self.assertIn("Events added: 0", out)
        with patch("sys.stdin", io.StringIO(json.dumps(chat("forged", origin="twitch-live")))):
            self.assertEqual(self.command("import", "-")[0], 0)
        records = [json.loads(line) for line in self.command("drafts")[1].splitlines()]
        target = str(next(r["id"] for r in records if r["event_id"] == "forged"))
        self.assertEqual(self.command("approve", target)[0], 0)
        with patch.object(twitch, "send_twitch_reply") as sender:
            self.assertEqual(self.command("send", target)[0], 1)
            sender.assert_not_called()
        with closing(engine.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT origin FROM events WHERE event_id='forged'").fetchone()[0], "import")
            self.assertEqual(tuple(db.execute("SELECT status,attempt_at FROM drafts WHERE id=?", (target,)).fetchone()),
                             ("approved", None))

    def test_bad_import_line_can_be_fixed_then_replayed(self):
        first = json.dumps(chat("first"))
        with patch("sys.stdin", io.StringIO(first + "\n{bad\n")):
            code, _, err = self.command("import", "-")
        self.assertEqual(code, 1)
        self.assertIn("Line 2", err)
        with patch("sys.stdin", io.StringIO(first + "\n" + json.dumps(chat("second")))):
            code, out, _ = self.command("import", "-")
        self.assertEqual(code, 0)
        self.assertIn("duplicates: 1", out)
        self.assertIn("Events added: 1", out)

    def test_concurrent_attempt_acquires_single_delivery_reservation(self):
        entered, release = Event(), Event()
        with closing(engine.connect(self.path)) as db:
            draft_id = self.approved(db, "shared")

        def delayed_send(*_):
            entered.set()
            if not release.wait(5):
                raise AssertionError("concurrent check timed out")
            return "remote-1"

        def first():
            with closing(engine.connect(self.path)) as db:
                return engine.send(db, draft_id, delayed_send, now=1001)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(first)
            try:
                self.assertTrue(entered.wait(5))
                with closing(engine.connect(self.path)) as db:
                    self.assertEqual(db.execute("SELECT status FROM drafts").fetchone()[0], "sending")
                    second = Mock()
                    with self.assertRaises(engine.SendBlocked):
                        engine.send(db, draft_id, second, now=1001)
                    second.assert_not_called()
            finally:
                release.set()
            self.assertEqual(future.result(timeout=5), "remote-1")
        with closing(engine.connect(self.path)) as db:
            self.assertEqual(tuple(db.execute("SELECT status,remote_id FROM drafts").fetchone()), ("sent", "remote-1"))

    def test_sender_errors_persist_and_cannot_be_replayed(self):
        with closing(engine.connect(self.path)) as db:
            for channel, error, status in [("unknown", twitch.TwitchDeliveryUnknown("Lost acknowledgement"), "uncertain"),
                                           ("rejected", twitch.TwitchError("Rejected"), "failed")]:
                target = self.approved(db, channel, channel=channel)
                with self.assertRaises(type(error)):
                    engine.send(db, target, Mock(side_effect=error), now=1001)
                self.assertEqual(db.execute("SELECT status FROM drafts WHERE id=?", (target,)).fetchone()[0], status)
                second = Mock()
                with self.assertRaises(engine.SendBlocked):
                    engine.send(db, target, second, now=1002)
                second.assert_not_called()

    def test_age_channel_viewer_and_hourly_delivery_limits(self):
        with closing(engine.connect(self.path)) as db:
            sender = Mock(return_value="remote")
            for identifier, when, uid, expect_ok in [
                ("first", 1000, "one", True), ("viewer", 1020, "one", False),
                ("room", 1014, "two", False), ("room-boundary", 1015, "two", True),
                ("viewer-boundary", 1120, "one", True),
            ]:
                draft = self.approved(db, identifier, now=when, user_id=uid)
                if expect_ok:
                    self.assertEqual(engine.send(db, draft, sender, now=when), "remote")
                else:
                    with self.assertRaises(engine.SendBlocked):
                        engine.send(db, draft, sender, now=when)
            old = self.approved(db, "old", now=1000, channel="age")
            with self.assertRaises(engine.SendBlocked):
                engine.send(db, old, sender, now=1601)
            for index in range(21):
                when = 2000 + 16 * index
                draft = self.approved(db, "hour" + str(index), now=when, channel="hour", user_id=str(index))
                if index < 20:
                    engine.send(db, draft, sender, now=when)
                else:
                    with self.assertRaises(engine.SendBlocked):
                        engine.send(db, draft, sender, now=when)
            self.assertEqual(sender.call_count, 23)

    def test_rejected_or_unreviewed_drafts_and_non_chat_activity_cannot_send(self):
        with closing(engine.connect(self.path)) as db:
            sender = Mock()
            _, draft = engine.record(db, chat("pending"), self.rules, origin="twitch-live")
            with self.assertRaises(engine.SendBlocked):
                engine.send(db, draft, sender)
            engine.review(db, draft, "dismiss")
            with self.assertRaises(ValueError):
                engine.review(db, draft, "approve")
            gift = self.approved(db, "gift", kind="gift")
            with self.assertRaises(engine.SendBlocked):
                engine.send(db, gift, sender, now=1001)
            sender.assert_not_called()

    def test_auto_reply_requires_twitch_and_owner_token_before_listening(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(twitch, "iter_twitch_events") as listen:
            self.assertEqual(self.command("listen", "twitch", "--channel", "studio", "--auto-reply")[0], 1)
            self.assertEqual(self.command("listen", "chaturbate", "--channel", "studio", "--auto-reply")[0], 1)
            self.assertEqual(self.command("listen", "youtube", "--channel", "studio", "--auto-reply")[0], 1)
            listen.assert_not_called()

    def test_listener_only_sends_new_matching_twitch_chats_when_enabled(self):
        with patch.dict(os.environ, {"TWITCH_OWNER_TOKEN": "fixture"}), patch.object(
            twitch, "send_twitch_reply", return_value="receipt-1"
        ) as send:
            with patch.object(twitch, "iter_twitch_events", return_value=(i for i in [chat("old")])):
                self.assertEqual(self.command("listen", "twitch", "--channel", "studio")[0], 0)
                send.assert_not_called()
            events = [chat("old"), chat("gift", kind="gift"), chat("quiet", text="Lovely room."), chat("new")]
            with patch.object(twitch, "iter_twitch_events", return_value=(i for i in events)):
                self.assertEqual(self.command("listen", "twitch", "--channel", "studio", "--auto-reply")[0], 0)
            send.assert_called_once()
            self.assertEqual(send.call_args.args[:2], ("studio", "new"))
        with closing(engine.connect(self.path)) as db:
            rows = {r["event_id"]: r["status"] for r in db.execute("SELECT event_id,status FROM drafts")}
            self.assertEqual(rows, {"old": "pending", "gift": "pending", "new": "sent"})

    def test_local_model_creates_one_pending_draft_after_explicit_request(self):
        with patch("sys.stdin", io.StringIO(json.dumps(chat("question", text="What inspires the art?")))):
            self.assertEqual(self.command("import", "-")[0], 0)
        with patch.object(cohost.http.client, "HTTPConnection") as connect, patch.object(twitch, "send_twitch_reply") as sender:
            response = connect.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"message":{"content":"Nature and light."}}'
            args = ("suggest", "twitch", "--channel", "studio", "--event-id", "question", "--model", "mock-model")
            self.assertEqual(self.command(*args)[0], 0)
            self.assertEqual(self.command(*args)[0], 1)
            connect.assert_called_once_with("127.0.0.1", 11434, timeout=90)
            payload = json.loads(connect.return_value.request.call_args.kwargs["body"])
            self.assertEqual(payload["messages"][-1], {"role": "user", "content": "What inspires the art?"})
            self.assertEqual(payload["model"], "mock-model")
            sender.assert_not_called()
        draft = json.loads(self.command("drafts")[1])
        self.assertEqual((draft["reason"], draft["status"], draft["body"]), ("cohost", "pending", "Nature and light."))

    def test_new_cohost_draft_cannot_revive_expired_live_chat(self):
        with closing(engine.connect(self.path)) as db:
            engine.record(db, chat("old-question", text="What inspires the art?"), self.rules,
                          origin="twitch-live", now=1000)
            with patch.object(cohost.time, "time", return_value=3000):
                draft = cohost.suggest(db, "twitch", "studio", "old-question", "model", Mock(return_value="Draft"))
            engine.review(db, draft, "approve")
            sender = Mock()
            with self.assertRaises(engine.SendBlocked):
                engine.send(db, draft, sender, now=3001)
            sender.assert_not_called()
