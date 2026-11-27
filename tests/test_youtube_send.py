import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit
from socialbot import youtube_send as yt


class YouTubeSendTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ",{"YOUTUBE_OWNER_TOKEN":"test-restricted-owner"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.active = {"snippet":{"liveChatId":"room-A","channelId":"channel-A"},"status":{"lifeCycleStatus":"live"}}
        self.mine = {"items":[{"id":"channel-A"}]}
        self.receipt = {"id":"youtube-message","snippet":{"liveChatId":"room-A","authorChannelId":"channel-A","type":"textMessageEvent"}}

    def api(self,*responses):
        stack = []
        for status,body in responses:
            c = MagicMock()
            r = c.getresponse.return_value
            r.status = status
            r.read.return_value = body if isinstance(body,bytes) else json.dumps(body).encode()
            stack.append(c)
        ctx = patch.object(yt.http.client,"HTTPSConnection",side_effect=stack)
        return ctx,stack

    def test_preflight_finds_active_chat_and_owns_channel_before_single_post(self):
        ctx,calls = self.api((200,{"items":[],"nextPageToken":"second"}),(200,{"items":[self.active]}),
                             (200,self.mine),(200,self.receipt))
        with ctx as client:
            self.assertEqual("youtube-message",yt.send_youtube_message("room-A","Thanks, Sam!"))
            self.assertEqual(4,client.call_count)
            self.assertTrue(all(call.args[0] == "www.googleapis.com" for call in client.call_args_list))
        actual = [c.request.call_args for c in calls]
        self.assertEqual(["GET","GET","GET","POST"],[c.args[0] for c in actual])
        self.assertEqual(["second"],parse_qs(urlsplit(actual[1].args[1]).query)["pageToken"])
        self.assertEqual(["true"],parse_qs(urlsplit(actual[2].args[1]).query)["mine"])
        self.assertEqual("/youtube/v3/liveChat/messages?part=snippet",actual[3].args[1])
        self.assertEqual({"liveChatId":"room-A","type":"textMessageEvent","textMessageDetails":{"messageText":"Thanks, Sam!"}},
                         json.loads(actual[3].kwargs["body"])["snippet"])
        self.assertTrue(all(c.kwargs["headers"]["Authorization"] == "Bearer test-restricted-owner" for c in actual))
        self.assertTrue(all("test-restricted-owner" not in c.args[1] for c in actual))

    def test_unowned_finished_or_mismatched_chat_never_posts(self):
        tests = [
            [(200,{"items":[self.active]}),(200,{"items":[{"id":"other"}]})],
            [(200,{"items":[dict(self.active,status={"lifeCycleStatus":"complete"})]})],
            [(200,{"items":[dict(self.active,snippet={"liveChatId":"other","channelId":"channel-A"})]})],
            [(401,b"private application data")],
            [(200,{"items":[],"nextPageToken":"a"}),(200,{"items":[],"nextPageToken":"a"})],
        ]
        for rows in tests:
            ctx,calls = self.api(*rows)
            with self.subTest(rows=rows),ctx,self.assertRaises(yt.PreflightError):
                yt.send_youtube_message("room-A","thanks")
            self.assertTrue(all(c.request.call_args.args[0] == "GET" for c in calls))

    def test_explicit_rejection_and_unconfirmed_deliveries_are_distinct(self):
        for status,body,exc in ((403,b"private",yt.SendRejected),(408,b"",yt.DeliveryUnknown),
                                (503,b"",yt.DeliveryUnknown),(200,b"bad!",yt.DeliveryUnknown),
                                (200,dict(self.receipt,snippet={"liveChatId":"other"}),yt.DeliveryUnknown)):
            ctx,calls=self.api((200,{"items":[self.active]}),(200,self.mine),(status,body))
            with self.subTest(status=status,body=body),ctx,self.assertRaises(exc) as cm:
                yt.send_youtube_message("room-A","thanks")
            if exc is yt.SendRejected:
                self.assertNotIsInstance(cm.exception,yt.DeliveryUnknown)
            self.assertEqual(1,sum(c.request.call_args.args[0] == "POST" for c in calls))

    def test_connection_loss_during_send_is_uncertain(self):
        ctx,calls=self.api((200,{"items":[self.active]}),(200,self.mine),(200,self.receipt))
        calls[-1].getresponse.side_effect=TimeoutError()
        with ctx,self.assertRaises(yt.DeliveryUnknown):
            yt.send_youtube_message("room-A","hello")

    def test_local_validation_never_sends_or_truncates(self):
        with patch.object(yt.http.client,"HTTPSConnection") as conn:
            for room,msg in (("","x"),("room\nx","x"),("room-A","x"*201),("room-A","msg\nsecond")):
                with self.subTest(msg=msg),self.assertRaises(yt.PreflightError):
                    yt.send_youtube_message(room,msg)
            with patch.dict("os.environ",{"YOUTUBE_OWNER_TOKEN":""}),self.assertRaises(yt.PreflightError):
                yt.send_youtube_message("room-A","hello")
            conn.assert_not_called()
