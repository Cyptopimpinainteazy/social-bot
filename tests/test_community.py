"""Community commands run on a disposable SQLite database and a mocked sender."""
import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from socialbot import community as c
from socialbot import engine, twitch


class CommunityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "rooms.db"
        self.db = engine.connect(self.path)
        self.addCleanup(self.db.close)
        self.rules = engine.load_rules()
        p = argparse.ArgumentParser()
        c.register(p.add_subparsers(dest="command", required=True))
        self.parser = p

    def event(self, eid, now, text="hello", *, kind="chat", platform="twitch", channel="one", origin=None):
        event = {"platform": platform, "channel": channel, "id": eid, "kind": kind,
                 "user": "Riv", "user_id": "42", "text": text}
        return engine.record(self.db, event, self.rules, now=now, origin=origin or platform + "-live")

    def cmd(self, *args, now=100):
        opts = self.parser.parse_args(["community", *args])
        out = io.StringIO()
        with patch.object(c.time, "time", return_value=now), redirect_stdout(out):
            c.run(opts, self.db)
        return out.getvalue().strip()

    def campaign(self, room="one", platform="twitch", text="Upcoming stream"):
        ident = self.cmd("campaign-add", platform, room, text)
        self.cmd("campaign-approve", ident)
        self.cmd("schedule", platform, room, "--interval-sec", "15", "--quiet-sec", "10", "--fresh-sec", "60")
        return int(ident)

    def queue(self, room="one", platform="twitch"):
        return [json.loads(i) for i in self.cmd("queue", platform, room).splitlines()]

    def test_only_exact_verified_own_consent_and_new_cycle_after_optout(self):
        self.event("imported", 1, "!optin", origin="import")
        self.event("space", 2, "maybe !optin")
        self.event("gift", 3, "!optin", kind="gift")
        self.assertEqual(self.cmd("members", "twitch", "one"), "")
        self.event("yes", 5, " !OPTIN ")
        self.event("yes", 6, "!optin")
        self.event("again", 7, "!optin")
        self.assertEqual(len(self.queue()), 1)
        self.assertEqual(json.loads(self.cmd("members", "twitch", "one"))["viewer_key"], "id:42")
        self.event("bye", 10, "!optout", channel="another")
        self.event("bad", 11, "!optout", origin="import")
        item = str(self.queue()[0]["id"])
        self.cmd("approve", item, now=12)
        self.event("bye", 13, "!optout")
        self.assertEqual(self.queue()[0]["status"], "revoked")
        with self.assertRaises(ValueError):
            self.cmd("copy", item, now=14)
        self.event("rejoin", 16, "!optin")
        self.assertEqual([q["status"] for q in self.queue()], ["pending", "revoked"])
        self.cmd("revoke", "twitch", "one", "id:42")
        self.assertEqual(self.cmd("members", "twitch", "one"), "")

    def test_tick_one_followup_per_consent_and_stale_items_expire(self):
        self.event("opt", 10, "!optin")
        self.event("tip", 12, "rose", kind="gift")
        self.assertEqual(c.tick(self.db, 10 + c.DAY - 1)["followups"], 0)
        self.assertEqual(c.tick(self.db, 10 + c.DAY)["followups"], 1)
        self.assertEqual(c.tick(self.db, 10 + c.DAY)["followups"], 0)
        rows = self.queue()
        self.assertEqual([r["status"] for r in rows], ["pending", "expired"])
        self.assertIn("supporting", rows[0]["body"])
        c.tick(self.db, 10 + 7 * c.DAY)
        self.assertEqual(self.queue()[0]["status"], "expired")
        with self.assertRaises(ValueError):
            self.cmd("approve", str(rows[0]["id"]), now=10 + 8 * c.DAY)

    def test_event_rollback_rolls_back_consent_and_greeting_together(self):
        self.db.execute("""CREATE TRIGGER force_queue_rollback BEFORE INSERT ON community_queue
                           BEGIN SELECT RAISE(ABORT, 'abort-for-test'); END""")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.event("atomic", 10, "!optin")
        for table in ("events", "community_members", "community_queue"):
            self.assertEqual(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_rotates_separately_per_room_with_recent_real_and_quiet_activity(self):
        first = self.campaign()
        second = self.cmd("campaign-add", "twitch", "one", "Partner", "--affiliate", "--link", "https://example.org/deal")
        self.cmd("campaign-approve", second)
        self.campaign("two")
        self.event("fake", 115, origin="import")
        self.assertEqual(c.tick(self.db, 120)["announcements"], 0)
        self.event("live", 120)
        self.assertEqual(c.tick(self.db, 125)["announcements"], 0)
        self.assertEqual(c.tick(self.db, 130)["announcements"], 1)
        self.assertEqual(c.tick(self.db, 146)["announcements"], 0)
        self.assertEqual(self.queue("two"), [])
        row = self.queue()[0]
        self.cmd("approve", str(row["id"]), now=145)
        self.assertEqual(self.cmd("copy", str(row["id"]), now=145), "Upcoming stream")
        self.assertEqual(c.tick(self.db, 146)["announcements"], 1)
        row = self.queue()[0]
        self.assertIn("Affiliate link: https://example.org/deal", row["body"])
        self.cmd("campaign-archive", second)
        self.assertEqual(self.queue()[0]["status"], "revoked")
        self.assertEqual(c.tick(self.db, 300)["announcements"], 0)
        self.assertEqual([r["status"] for r in map(json.loads, self.cmd("campaigns", "twitch", "one").splitlines())],
                         ["approved", "archived"])
        self.assertNotEqual(first, int(second))

    def test_copy_rechecks_room_and_invalid_affiliate_schemes_are_rejected(self):
        with self.assertRaises(ValueError):
            self.cmd("campaign-add", "twitch", "one", "Offer", "--affiliate", "--link", "javascript:alert(1)")
        self.campaign()
        self.event("active", 120)
        c.tick(self.db, 130)
        target = str(self.queue()[0]["id"])
        with self.assertRaises(ValueError):
            self.cmd("copy", target, now=130)
        self.cmd("approve", target, now=130)
        self.event("fresh", 131)
        with self.assertRaises(ValueError):
            self.cmd("copy", target, now=132)
        with self.assertRaises(ValueError):
            self.cmd("copy", target, now=191)

    def test_remote_sender_requires_current_individual_approval_and_confirmation(self):
        self.campaign()
        self.event("active", 120)
        c.tick(self.db, 130)
        target = self.queue()[0]["id"]
        sender = Mock(return_value="ack")
        with self.assertRaises(ValueError):
            c.send(self.db, target, sender, 130)
        self.cmd("approve", str(target), now=130)
        self.event("active2", 131)
        with self.assertRaises(ValueError):
            c.send(self.db, target, sender, 132)
        sender.assert_not_called()
        self.assertEqual(c.send(self.db, target, sender, 142), "ack")
        with self.assertRaises(ValueError):
            c.send(self.db, target, sender, 143)
        sender.assert_called_once_with("one", "Upcoming stream")
        self.assertEqual(self.queue()[0]["status"], "sent")
        c.tick(self.db, 146)
        target2 = self.queue()[0]["id"]
        self.cmd("approve", str(target2), now=146)
        with self.assertRaises(ValueError):
            c.send(self.db, target2, sender, 146)
        sender.assert_called_once()
        self.assertEqual(self.db.execute("SELECT remote_id FROM community_deliveries").fetchone()[0], "ack")

    def test_distinguishes_definite_preflight_and_unknown_outcomes(self):
        self.campaign()
        self.event("active", 120)
        c.tick(self.db, 130)
        target = self.queue()[0]["id"]
        self.cmd("approve", str(target), now=130)
        with self.assertRaises(twitch.TwitchPreflightError):
            c.send(self.db, target, Mock(side_effect=twitch.TwitchPreflightError("missing token")), 130)
        self.assertEqual(self.queue()[0]["status"], "approved")
        with self.assertRaises(twitch.TwitchDeliveryUnknown):
            c.send(self.db, target, Mock(side_effect=twitch.TwitchDeliveryUnknown("ack lost")), 131)
        self.assertEqual(self.queue()[0]["status"], "uncertain")
        retry = Mock()
        with self.assertRaises(ValueError):
            c.send(self.db, target, retry, 140)
        retry.assert_not_called()

    def test_personal_entries_and_other_platforms_cannot_be_sent_as_announcements(self):
        self.campaign(platform="youtube")
        self.event("active", 120, platform="youtube")
        self.event("opt", 120, "!optin")
        c.tick(self.db, 130)
        sender = Mock()
        for q in (self.queue()[0], self.queue(platform="youtube")[0]):
            self.cmd("approve", str(q["id"]), now=130)
            with self.assertRaises(ValueError):
                c.send(self.db, q["id"], sender, 131)
        sender.assert_not_called()

    def test_concurrent_ticks_and_sends_share_single_sqlite_reservation(self):
        from concurrent.futures import ThreadPoolExecutor
        from contextlib import closing
        from threading import Event

        self.campaign()
        self.event("active", 120)
        def worker_tick():
            with closing(engine.connect(self.path)) as db:
                return c.tick(db, 130)["announcements"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker_tick) for _ in range(2)]
            self.assertEqual(sum(f.result(timeout=5) for f in futures), 1)
            item = self.queue()[0]["id"]
            self.cmd("approve", str(item), now=130)
            entered, release = Event(), Event()

            def outbound(*_):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Overlapping send timed out")
                return "ack"

            def worker_send():
                with closing(engine.connect(self.path)) as db:
                    return c.send(db, item, outbound, 130)

            pending = pool.submit(worker_send)
            try:
                self.assertTrue(entered.wait(5))
                another = Mock()
                with self.assertRaises(ValueError):
                    c.send(self.db, item, another, 131)
                another.assert_not_called()
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=5), "ack")

    def test_abandoned_send_becomes_uncertain_without_another_attempt(self):
        self.campaign()
        self.event("active", 120)
        c.tick(self.db, 130)
        item = self.queue()[0]["id"]
        self.cmd("approve", str(item), now=130)
        # Process interruption after reserving, before recording a response.
        with self.assertRaises(KeyboardInterrupt):
            c.send(self.db, item, Mock(side_effect=KeyboardInterrupt), 130)
        self.assertEqual(self.queue()[0]["status"], "sending")
        c.tick(self.db, 731)
        self.assertEqual(self.queue()[0]["status"], "uncertain")
        sender = Mock()
        with self.assertRaises(ValueError):
            c.send(self.db, item, sender, 732)
        sender.assert_not_called()

    def test_followback_is_requested_by_sender_separately_from_contact_consent(self):
        self.event("forged", 1, "!followback", origin="import")
        self.event("kick", 1, "!followback", platform="kick")
        self.assertEqual(self.cmd("followbacks", "twitch", "one"), "")
        self.event("request", 3, "!followback")
        self.event("repeat", 4, "!followback")
        self.assertEqual(json.loads(self.cmd("followbacks", "twitch", "one"))["asked_at"], 3)
        self.assertEqual(self.cmd("members", "twitch", "one"), "")
        self.event("wrong", 5, "!cancelfollowback", channel="two")
        self.event("ours", 6, "!cancelfollowback")
        self.assertEqual(self.cmd("followbacks", "twitch", "one"), "")
        self.event("again", 7, "!followback")
        future = 7 + 7 * c.DAY
        self.assertEqual(self.cmd("followbacks", "twitch", "one", now=future), "")
        with self.assertRaises(ValueError):
            self.cmd("followback-mark", "twitch", "one", "id:42", "followed", now=future)
        self.event("renew", future + 1, "!followback")
        self.cmd("followback-mark", "twitch", "one", "id:42", "followed", now=future + 2)
        self.event("already", future + 3, "!followback")
        self.assertEqual(json.loads(self.cmd("followbacks", "twitch", "one", "--status", "followed"))["name"], "Riv")
        self.assertEqual(self.cmd("followbacks", "twitch", "one"), "")

    def test_publish_watcher_is_scoped_and_preserves_manual_and_personal_items(self):
        self.campaign()
        self.campaign("two")
        self.event("active", 120)
        self.event("active", 120, channel="two")
        self.event("opt", 120, "!optin")
        sender = Mock(return_value="scheduled-ack")
        self.assertEqual(c.watch_once(self.db, sender=sender, now=130)["announcements"], 2)
        sender.assert_not_called()
        row = next(i for i in self.queue("two") if i["kind"] == "announcement")
        self.cmd("dismiss", str(row["id"]), now=130)
        self.assertNotIn("publish", c.watch_once(self.db, "two", sender=sender, now=131))
        self.assertEqual(c.watch_once(self.db, "one", sender=sender, now=131)["publish"], "sent")
        self.assertNotIn("publish", c.watch_once(self.db, "one", sender=sender, now=132))
        sender.assert_called_once_with("one", "Upcoming stream")
        self.assertEqual({i["kind"]: i["status"] for i in self.queue()},
                         {"announcement": "sent", "welcome": "pending"})
        self.assertEqual(self.queue("two")[0]["status"], "dismissed")

    def test_watch_holds_busy_expired_archived_and_uncertain_campaigns(self):
        campaign = self.campaign()
        self.event("active", 120)
        c.tick(self.db, 130)
        self.event("busy", 131)
        sender = Mock(side_effect=twitch.TwitchDeliveryUnknown("ack lost"))
        self.assertEqual(c.watch_once(self.db, "one", sender=sender, now=132)["publish"], "held")
        sender.assert_not_called()
        self.assertEqual(c.watch_once(self.db, "one", sender=sender, now=141)["publish"], "uncertain")
        self.event("later", 230)
        self.assertEqual(c.watch_once(self.db, "one", sender=sender, now=240)["publish"], "held")
        self.cmd("campaign-archive", str(campaign), now=240)
        self.assertNotIn("publish", c.watch_once(self.db, "one", sender=sender, now=241))
        sender.assert_called_once()

    def test_publish_watch_requires_owner_token_at_cli_before_starting(self):
        with patch.dict(c.os.environ, {}, clear=True), patch.object(c, "watch_once") as step:
            with self.assertRaisesRegex(ValueError, "TWITCH_OWNER_TOKEN"):
                self.cmd("watch", "--publish-twitch", "one")
            step.assert_not_called()
