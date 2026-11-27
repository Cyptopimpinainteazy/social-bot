import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from socialbot import engine, ops


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.db_path = self.home/"main.sqlite3"
        self.room = {"name":"studio","platform":"twitch","channel":"owner"}
        self.alerts = {"events":["gift","error"],"destinations":["discord","telegram"]}
        self.args = argparse.Namespace(db=self.db_path,rules=engine.DEFAULT_RULES,watchdog=600)
        self.state = ops._init_state(self.home/"state.sqlite3")
        self.addCleanup(self.state.close)

    def read_config(self, data):
        target = self.home/"config.json"
        target.write_text(json.dumps(data))
        return ops.load_config(target)

    def test_validate_config_without_network_and_isolate_credentials(self):
        rooms,watchdog,alerts = self.read_config({"rooms":[self.room]})
        self.assertEqual(([self.room],600,{}),(rooms,watchdog,alerts))
        room = dict(self.room,env={"TWITCH_OAUTH_TOKEN":"STUDIO_READ"})
        with patch.dict(os.environ,{"TWITCH_BOT_LOGIN":"host","STUDIO_READ":"fake","TWITCH_OWNER_TOKEN":"owner",
                                   "DISCORD_WEBHOOK_URL":"webhook","EXTRA_SECRET":"private"},clear=True):
            self.assertEqual({"TWITCH_BOT_LOGIN":"host","TWITCH_OAUTH_TOKEN":"fake"},ops.worker_env(room))
        for change in ({"rooms":[self.room,self.room]}, {"rooms":[dict(self.room,env={"TWITCH_OAUTH_TOKEN":"raw/token"})]},
                       {"rooms":[dict(self.room,platform="youtube",live_chat_id="x",auto_reply=True)]},
                       {"rooms":[self.room],"alerts":{"destinations":["unknown"]}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.read_config(change)

    def test_watchdog_restart_and_alert_once_per_outage(self):
        pulse = self.home/"worker.pulse"
        w = ops.Worker(self.room,{},pulse)
        proc = Mock(pid=211)
        proc.poll.return_value = None
        with patch.object(ops.subprocess,"Popen",return_value=proc), patch.object(ops.time,"time",return_value=1000):
            ops._advance(w,self.state,self.args,self.alerts,78,1000)
            self.assertEqual("starting",w.state)
            ops._advance(w,self.state,self.args,self.alerts,78,1610)
            self.assertGreater(w.retry_at,1610)
            ops._advance(w,self.state,self.args,self.alerts,78,1611)
            self.assertIsNone(w.process)
            ops._advance(w,self.state,self.args,self.alerts,78,1620)
            proc.poll.return_value = 1
            ops._advance(w,self.state,self.args,self.alerts,78,1621)
            self.assertEqual(2,w.restarts)
            self.assertEqual(2,self.state.execute("SELECT count(*) FROM alerts").fetchone()[0])
        self.assertTrue(proc.terminate.called)

    def test_quiet_feed_health_then_clean_end(self):
        w = ops.Worker(self.room,{},self.home/"pulse")
        w.process = Mock(pid=211)
        w.process.poll.return_value = None
        w.started = time.monotonic()-700
        with patch.dict(os.environ,{"SOCIALBOT_HEARTBEAT":str(w.heartbeat)}),patch.object(ops,"last_pulse",None):
            ops.pulse()
        ops._advance(w,self.state,self.args,self.alerts,0,time.monotonic())
        self.assertEqual("running",w.state)
        w.process.poll.return_value = 0
        ops._advance(w,self.state,self.args,self.alerts,0,time.monotonic())
        self.assertEqual("ended",w.state)
        self.assertEqual(0,w.restarts)

    def test_gift_privacy_dedup_and_terminal_delivery(self):
        with closing(engine.connect(self.db_path)) as events:
            self.state.execute("INSERT INTO cursor VALUES(1,0)")
            self.state.commit()
            rules = engine.load_rules()
            gift = {"platform":"twitch","channel":"owner","user":"private-name","id":"private-id",
                    "kind":"gift","text":"private amount"}
            for eid,origin in (("private-id","twitch-live"),("mock","import")):
                engine.record(events,dict(gift,id=eid),rules,origin=origin)
            ops._capture_gifts(self.state,events,[self.room],self.alerts)
            with self.state:
                self.state.execute("UPDATE cursor SET last_event=0")
            ops._capture_gifts(self.state,events,[self.room],self.alerts)
        self.assertEqual(2,self.state.execute("SELECT count(*) FROM alerts").fetchone()[0])
        sender = Mock(return_value="uncertain")
        self.assertTrue(ops._dispatch_one(self.state,["discord"],sender))
        self.assertFalse(ops._dispatch_one(self.state,["discord"],sender))
        sender.assert_called_once_with("discord","Gift received · studio (twitch)")
        self.assertNotIn("private",str([tuple(r) for r in self.state.execute("SELECT * FROM alerts")]))

    def test_destinations_use_fixed_hosts_and_redact_secrets(self):
        with patch.dict(os.environ,{"DISCORD_WEBHOOK_URL":"https://example.org/api/webhooks/123/SUPERSECRET"}):
            with self.assertRaisesRegex(ValueError,"DISCORD_WEBHOOK_URL") as cm:
                ops._destination("discord")
            self.assertNotIn("SUPERSECRET",str(cm.exception))
        conn = Mock()
        conn.getresponse.return_value.status = 200
        conn.getresponse.return_value.read.return_value = b'{"id":"abc"}'
        with patch.dict(os.environ,{"DISCORD_WEBHOOK_URL":"https://discord.com/api/webhooks/123/mock"}), \
             patch.object(ops.http.client,"HTTPSConnection",return_value=conn) as mock_client:
            self.assertEqual("sent",ops._deliver("discord","Test"))
            mock_client.assert_called_once_with("discord.com",timeout=5)
            self.assertEqual({"parse":[]},json.loads(conn.request.call_args.args[2])["allowed_mentions"])
        with patch.dict(os.environ,{"TELEGRAM_BOT_TOKEN":"123:fake","TELEGRAM_CHAT_ID":"-123"}), \
             patch.object(ops.http.client,"HTTPSConnection",return_value=conn):
            conn.getresponse.return_value.read.return_value = b'{"ok":true}'
            self.assertEqual("sent",ops._deliver("telegram","Test"))
            self.assertTrue(json.loads(conn.request.call_args.args[2])["protect_content"])

    def test_same_subscription_resumes_new_recipient_expires_pending(self):
        with closing(engine.connect(self.db_path)) as events, patch.dict(os.environ,{
                "DISCORD_WEBHOOK_URL":"https://discord.com/api/webhooks/123/mock",
                "TELEGRAM_BOT_TOKEN":"123:fake","TELEGRAM_CHAT_ID":"-123"}):
            ops._resume_alerts(self.state, events, [self.room], self.alerts)
            with self.state:
                ops._queue_alert(self.state,self.alerts,"gift",("abc",),self.room)
            ops._resume_alerts(self.state, events, [self.room], self.alerts)
            self.assertEqual(2,self.state.execute("SELECT count(*) FROM alerts WHERE state='pending'").fetchone()[0])
            with patch.dict(os.environ,{"TELEGRAM_CHAT_ID":"-456"}):
                ops._resume_alerts(self.state, events, [self.room], self.alerts)
            self.assertEqual(2,self.state.execute("SELECT count(*) FROM alerts WHERE state='expired'").fetchone()[0])

    def test_rate_limited_alert_waits_until_provider_delay(self):
        with self.state:
            ops._queue_alert(self.state,self.alerts,"gift",("abc",),self.room)
        with patch.object(ops.time,"time",return_value=100):
            self.assertTrue(ops._dispatch_one(self.state,["discord"],lambda *_:12))
            self.assertFalse(ops._dispatch_one(self.state,["discord"],lambda *_:"sent"))
        with patch.object(ops.time,"time",return_value=113):
            self.assertTrue(ops._dispatch_one(self.state,["discord"],lambda *_:"sent"))
        record = self.state.execute("SELECT state,retries FROM alerts WHERE destination='discord'").fetchone()
        self.assertEqual(("sent",1),tuple(record))

    @unittest.skipUnless(os.name == "posix","POSIX fleet")
    def test_supervisor_status_backoff_singleton_and_stop(self):
        # The deliberately invalid bot login fails locally before a socket opens.
        cfg = self.home/"fleet.json"
        cfg.write_text(json.dumps({"rooms":[self.room]}))
        script = ("from socialbot import ops,engine; from argparse import Namespace; "
                  "from pathlib import Path; import sys; "
                  "sys.exit(ops.run(Namespace(command='fleet',check=False,rules=engine.DEFAULT_RULES,"
                  "db=Path(sys.argv[1]), config=Path(sys.argv[2]))))")
        env = {**os.environ,"TWITCH_BOT_LOGIN":"bad login","TWITCH_OAUTH_TOKEN":"dummy"}
        command = [sys.executable,"-c",script,str(self.db_path),str(cfg)]
        child = subprocess.Popen(command,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            until = time.monotonic()+10
            while time.monotonic()<until:
                rows = ops.dashboard_status(self.db_path)
                if rows and rows[0]["state"]=="backoff":
                    break
                time.sleep(0.05)
            else:
                self.fail("Supervisor did not register failure and enter restart backoff")
            second = subprocess.run(command,env=env,capture_output=True,text=True,timeout=5)
            self.assertNotEqual(0,second.returncode)
            self.assertIn("Another supervisor",second.stderr)
            child.send_signal(signal.SIGTERM)
            self.assertEqual(0,child.wait(timeout=6))
            self.assertEqual("stopped",ops.dashboard_status(self.db_path)[0]["state"])
        finally:
            child.kill()
            child.communicate(timeout=5)

    def test_status_missing_db_then_stale(self):
        self.assertEqual([],ops.dashboard_status(self.db_path))
        directory = ops.state_dir(self.db_path)
        self.assertFalse(directory.exists())
        directory.mkdir()
        with closing(ops._init_state(directory/"status.sqlite3")) as status:
            with status:
                status.execute("INSERT INTO workers VALUES('alias','twitch','running',1,2,0,0)")
        record = ops.dashboard_status(self.db_path)[0]
        self.assertEqual("supervisor stale",record["state"])
        self.assertIsNone(record["pid"])

    @unittest.skipUnless(os.name == "posix","POSIX fleet")
    def test_real_subprocess_uses_normal_cli_and_exits_with_supervisor(self):
        # Invalid channel at the worker CLI exits before making a network request.
        read_fd,write_fd = os.pipe()
        try:
            cmd = [sys.executable,"-m","socialbot.ops",str(read_fd),"--db",str(self.db_path),
                   "listen","twitch","--channel","invalid user"]
            child = subprocess.run(cmd,pass_fds=(read_fd,),capture_output=True,text=True,timeout=10)
            self.assertEqual(1,child.returncode)
            self.assertIn("Twitch login",child.stderr)
            # The dashboard has no external connections and would otherwise keep running.
            cmd = [sys.executable,"-m","socialbot.ops",str(read_fd),"--db",str(self.db_path),"dashboard","--port","0"]
            child = subprocess.Popen(cmd,pass_fds=(read_fd,),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                os.close(write_fd)
                write_fd = None
                self.assertEqual(0,child.wait(timeout=5))
            finally:
                child.kill()
                child.communicate(timeout=5)
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
