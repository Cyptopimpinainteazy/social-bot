"""Regression checks for the public creator workflow."""
import http.client
import json
import tempfile
import threading
import time
import unittest
from contextlib import closing, contextmanager
from http.server import HTTPServer, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from socialbot import bridge, cohost, controls, dashboard, engine, telegram, twitch

@contextmanager
def local_server(factory, handler):
    with factory(("127.0.0.1",0),handler) as server:
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            thread.join(3)

def post(port,path,body,headers):
    with closing(http.client.HTTPConnection("127.0.0.1",port,timeout=3)) as conn:
        conn.request("POST",path,body,headers)
        response=conn.getresponse()
        return response.status,response.read()

def event(i, **updates):
    return dict({"id":str(i),"platform":"chaturbate","channel":"studio","user":"River",
                 "kind":"chat","text":"hello"},**updates)

class CreatorToolsTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir=Path(temp.name)
        self.path=self.dir/"main.sqlite3"
        self.db=engine.connect(self.path)
        self.addCleanup(self.db.close)
        self.cfg=json.loads(engine.DEFAULT_RULES.read_text())

    def test_tiers_author_variants_and_hot_reload(self):
        rules_file=self.dir/"rules.json"
        rules_file.write_text(json.dumps(self.cfg))
        active=engine.LiveRules(rules_file)
        self.cfg["gift_tiers"]=[{"platform":"chaturbate","unit":"tokens","minimum":10,
                                 "reply":["Big thanks, {user}","Thank you, {user}"]}]
        rules_file.write_text(json.dumps(self.cfg))
        engine.record(self.db,event(1,kind="gift",gift_amount=10,gift_unit="tokens"),active())
        engine.record(self.db,event(2,kind="gift",gift_amount=500,gift_unit="bits"),active())
        names=[row[0] for row in self.db.execute("SELECT reason FROM drafts ORDER BY id")]
        self.assertEqual(names,["gift_tier","gift"])
        self.assertEqual(engine.select_template(("a","b"),"same"),engine.select_template(("a","b"),"same"))
        rules_file.write_text('{"broken":')
        with patch("sys.stderr"):
            self.assertEqual(active()["gift_tiers"][0][2],10)
        with self.assertRaises(ValueError):
            engine.compile_rules(dict(self.cfg,first_join=["{user.__class__}"]))

    def test_scoped_bridge_deduplicates_and_rejects_impersonated_origin(self):
        # HTTPServer receives on its worker thread. Use a database opened on that thread.
        secret=b"x"*32
        class LocalDB:
            def __init__(self): self.local=threading.local()
            def connection(self):
                if not hasattr(self.local,"db"): self.local.db=engine.connect(self_path)
                return self.local.db
            def __getattr__(self,key): return getattr(self.connection(),key)
            def __enter__(self): return self.connection().__enter__()
            def __exit__(self,*args): return self.connection().__exit__(*args)
        self_path=self.path
        recv=bridge.handler(LocalDB(),engine.load_rules,"twitch","studio",secret)
        with local_server(HTTPServer,recv) as port:
            def submit(obj, signer=secret, offset=0):
                body=json.dumps(obj).encode()
                stamp=str(int(time.time())+offset)
                return post(port,bridge.PATH,body,{"X-SocialBot-Time":stamp,
                                "X-SocialBot-Signature":bridge.signature(signer,stamp,body)})[0]
            data=event(1,platform="twitch",origin="twitch-live")
            self.assertEqual(submit(data),201)
            self.assertEqual(submit(data),200)
            self.assertEqual(submit(data,offset=-500),401)
            self.assertEqual(submit(data,signer=b"no"),401)
            self.assertEqual(submit(dict(data,channel="other")),403)
        self.assertEqual(self.db.execute("SELECT origin FROM events").fetchone()[0],"creator-bridge")
        engine.review(self.db,1,"approve")
        with self.assertRaises(engine.SendBlocked):
            engine.send(self.db,1,Mock(side_effect=AssertionError))

    def test_dashboard_settings_review_and_readonly_account(self):
        rule_path=self.dir/"creator.json"
        ctl=controls.Controls(self.path,edit_rules=rule_path,review=True)
        engine.record(self.db,event(1),engine.load_rules())
        with local_server(ThreadingHTTPServer,dashboard._handler(self.path,ctl)) as port:
            path="/control/rules"
            addr=f"http://127.0.0.1:{port}"
            headers={"Origin":addr,"Content-Type":"application/x-www-form-urlencoded"}
            old=rule_path.read_bytes()
            revision=controls.revision(old)
            def form(obj): return urlencode(obj).encode()
            change={"token":ctl.token,"version":revision,"settings":json.dumps(dict(self.cfg,persona="Brief."))}
            self.assertEqual(post(port,path,form(change),dict(headers,Origin="https://example.com"))[0],403)
            self.assertEqual(post(port,path,form(dict(change,token="bad")),headers)[0],403)
            self.assertEqual(post(port,path,form(dict(change,settings='{}')),headers)[0],400)
            self.assertEqual(rule_path.read_bytes(),old)
            self.assertEqual(post(port,path,form(change),headers)[0],303)
            self.assertEqual(post(port,path,form(change),headers)[0],400)
            draft_body=self.db.execute("SELECT body FROM drafts WHERE id=1").fetchone()[0]
            form_data={"token":ctl.token,"id":"1","action":"approve","draft_version":controls.revision(draft_body.encode()),"body":draft_body}
            self.assertEqual(post(port,"/control/review",form(dict(form_data,body="Edited")),headers)[0],400)
            self.assertEqual(post(port,"/control/review",form(form_data),headers)[0],303)
        self.assertEqual(self.db.execute("SELECT status FROM drafts").fetchone()[0],"approved")
        self.assertEqual(engine.load_rules(rule_path)["persona"],"Brief.")
        body=dashboard.render_dashboard(self.path,ctl,[("another",self.dir/"missing.db")])
        self.assertIn("Unavailable",body)
        self.assertFalse((self.dir/"missing.db").exists())

    def test_cohost_worker_only_selects_recent_native_unanswered_questions(self):
        from socialbot.__main__ import parser
        args=parser().parse_args(["cohost-worker","twitch","--channel","studio","--once"])
        rules=engine.load_rules()
        for i,origin,when in [(1,"import",1000),(2,"twitch-live",20),(3,"twitch-live",1000)]:
            engine.record(self.db,event(i,platform="twitch",text="Why?"),rules,origin=origin,now=when)
        with patch.object(cohost.time,"time",return_value=1000), patch.object(cohost,"suggest",return_value=2) as draft:
            cohost.work(args,self.db)
        self.assertEqual(draft.call_args.args[3],"3")

    def test_telegram_private_initiator_and_restart_dedup(self):
        telegram.init(self.db)
        def update(i,command,actor=42,kind="private"):
            return {"update_id":i,"message":{"chat":{"id":42,"type":kind},"from":{"id":actor},"text":command}}
        send=Mock(return_value={"message_id":900})
        updates=[update(1,"/start",kind="group"), update(2,"/start",actor=11),update(3,"hi"),
                 update(4,"/start invite"),update(5,"/start")]
        telegram.consume(self.db,7,updates,"Welcome",send)
        telegram.consume(self.db,7,updates,"Welcome",send)
        send.assert_called_once_with(42,"Welcome")
        telegram.consume(self.db,7,[update(6,"/stop")],"Welcome",send)
        self.assertEqual(self.db.execute("SELECT count(*) FROM telegram_members").fetchone()[0],0)
        send.side_effect=TimeoutError
        telegram.consume(self.db,7,[update(7,"/start"),update(8,"/start")],"Welcome",send)
        self.assertEqual(send.call_count,2)
        self.assertEqual(self.db.execute("SELECT welcome_status FROM telegram_members").fetchone()[0],"uncertain")

    def test_announcements_share_chat_reply_rate_limit(self):
        now=int(time.time())
        _,draft=engine.record(self.db,event(1,platform="twitch"),engine.load_rules(),origin="twitch-live",now=now)
        engine.review(self.db,draft,"approve")
        self.db.execute("""INSERT INTO community_queue(platform,channel,kind,body) VALUES('twitch','studio','announcement','hi')""")
        self.db.execute("INSERT INTO community_deliveries VALUES(1,'twitch','studio',?,NULL)",(now-3,))
        self.db.commit()
        with self.assertRaises(engine.SendBlocked):
            engine.send(self.db,draft,Mock(side_effect=AssertionError),now=now)

    def test_routing_youtube_to_its_saved_broadcast_and_editor_reapproval(self):
        from socialbot import __main__ as cli
        from socialbot import youtube_send
        from contextlib import redirect_stdout,redirect_stderr
        import io
        args=["--db",str(self.path)]
        native=event("chat",platform="youtube",channel="local-label",live_chat_id="upstream-chat")
        _,draft=engine.record(self.db,native,engine.load_rules(),origin="youtube-live")
        engine.review(self.db,draft,"approve")
        self.assertEqual(engine.edit_draft(self.db,draft,"Welcome back"),None)
        with patch.object(youtube_send,"send_youtube_message",return_value="yt-receipt") as sender, \
                redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(args+["send",str(draft)]),1)
            self.assertEqual(cli.main(args+["approve",str(draft)]),0)
            self.assertEqual(cli.main(args+["send",str(draft)]),0)
            sender.assert_called_once_with("upstream-chat","Welcome back")
            _,fake=engine.record(self.db,dict(native,id="forged"),engine.load_rules(),origin="creator-bridge")
            engine.review(self.db,fake,"approve")
            self.assertEqual(cli.main(args+["send",str(fake)]),1)
            self.assertEqual(sender.call_count,1)
        self.assertEqual(self.db.execute("SELECT remote_id FROM drafts WHERE id=?",(draft,)).fetchone()[0],"yt-receipt")

    def test_returning_supporter_alert_needs_a_prior_native_gift(self):
        from socialbot import ops
        with closing(ops._init_state(self.dir/"state.db")) as state:
            state.execute("INSERT INTO cursor VALUES(1,0)")
            alerts={"events":["returning_supporter"],"destinations":["discord"]}
            room={"platform":"chaturbate","channel":"studio","name":"Evening show"}
            rules=engine.load_rules()
            for i,kind,origin in [(1,"join","chaturbate-live"),(2,"gift","import"),(3,"join","chaturbate-live"),
                                  (4,"gift","chaturbate-live"),(5,"join","chaturbate-live")]:
                engine.record(self.db,event(i,kind=kind),rules,origin=origin)
            ops._capture_gifts(state,self.db,[room],alerts)
            ops._capture_gifts(state,self.db,[room],alerts)
            rows=state.execute("SELECT message FROM alerts").fetchall()
            self.assertEqual(len(rows),1)
            self.assertNotIn("River",rows[0][0])

    def test_helix_announcement_has_no_reply_id(self):
        owner={"login":"studio","client_id":"a","user_id":"2","scopes":["user:write:chat"]}
        with patch.object(twitch,"_token",return_value="fixture"), patch.object(twitch,"_validate",return_value=owner), \
                patch.object(twitch,"_request_json",return_value={"data":[{"is_sent":True,"message_id":"m"}]}) as send:
            self.assertEqual(twitch.send_twitch_announcement("studio","Live now"),"m")
            self.assertEqual(json.loads(send.call_args.args[0].data),{"broadcaster_id":"2","sender_id":"2","message":"Live now"})
