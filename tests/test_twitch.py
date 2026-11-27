import io
import json
import os
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from socialbot import twitch

MESSAGE = "bbe96a4e-6bb8-414c-b62b-5c8eb402ff33"
CHAT = f"@id={MESSAGE};user-id=981;room-id=123 :viewer!viewer@example PRIVMSG #stage :hello!"
OWNER = {"client_id": "app", "user_id": "123", "login": "stage", "scopes": ["user:write:chat"]}


class TwitchEvents(unittest.TestCase):
    def test_tagged_native_events(self):
        event = twitch.parse_event(CHAT, "stage", "helper")
        self.assertEqual(event, {"id": MESSAGE, "user_id": "981", "platform": "twitch", "user": "viewer",
                                 "kind": "chat", "channel": "stage", "text": "hello!"})
        cheer = twitch.parse_event(CHAT.replace("@id=", "@bits=25;id="), "stage", "helper")
        self.assertEqual((cheer["kind"], cheer["text"]), ("gift", "25 Bits"))
        sub = f"@login=viewer;user-id=981;id={MESSAGE};msg-id=subgift :tmi.twitch.tv USERNOTICE #stage"
        self.assertEqual(twitch.parse_event(sub, "stage", "helper")["kind"], "gift")
        self.assertIsNone(twitch.parse_event(sub.replace("subgift", "modiversary"), "stage", "helper"))

    def test_channel_identity_and_self_filter(self):
        changes = [("PRIVMSG #stage", "PRIVMSG #other"), (":viewer!", ":helper!"),
                   (":viewer!", ":stage!"), (":viewer!", ":jtv!"),
                   ("user-id=981", "user-id="), ("@id=", "@source-room-id=666;id="),
                   (MESSAGE, "missing")]
        for left, right in changes:
            with self.subTest(right=right):
                self.assertIsNone(twitch.parse_event(CHAT.replace(left, right), "stage", "helper"))

    @patch.object(twitch.ssl, "create_default_context")
    @patch.object(twitch.socket, "create_connection")
    def test_irc_session_auth_keepalive_and_message(self, connect, context):
        stream = context.return_value.wrap_socket.return_value.__enter__.return_value
        # Protocol-looking chat text must not disconnect a listener.
        lines = [":tmi.twitch.tv 001 helper :Welcome", "PING :tmi.twitch.tv",
                 CHAT.replace("hello!", ":tmi.twitch.tv RECONNECT"), ":tmi.twitch.tv RECONNECT"]
        stream.makefile.return_value = io.BytesIO(("\r\n".join(lines) + "\r\n").encode())
        events = list(twitch._session("stage", "helper", "test-token"))
        self.assertEqual([e["id"] for e in events], [MESSAGE])
        sent = [c.args[0] for c in stream.sendall.call_args_list]
        self.assertIn(b"NICK helper\r\n", sent[0])
        self.assertIn(b"JOIN #stage\r\n", sent)
        self.assertIn(b"PONG :tmi.twitch.tv\r\n", sent)
        connect.assert_called_once_with(("irc.chat.twitch.tv", 6697), timeout=15)
        self.assertEqual(context.return_value.wrap_socket.call_args.kwargs["server_hostname"],
                         "irc.chat.twitch.tv")

    @patch.dict(os.environ, {"TWITCH_BOT_LOGIN": "helper", "TWITCH_OAUTH_TOKEN": "test-token"})
    @patch.object(twitch, "_session")
    @patch.object(twitch, "_request_json")
    def test_read_scope_and_account_checked_before_irc(self, request, session):
        request.return_value = dict(OWNER, login="helper")
        with self.assertRaisesRegex(twitch.TwitchError, "chat:read"):
            next(twitch.iter_twitch_events("stage"))
        session.assert_not_called()


class TwitchSend(unittest.TestCase):
    @patch.dict(os.environ, {"TWITCH_OWNER_TOKEN": "test-token"})
    @patch.object(twitch, "urlopen")
    def test_honors_reviewed_body_and_returns_exact_receipt(self, open_url):
        open_url.side_effect = [io.BytesIO(json.dumps(OWNER).encode()),
                                io.BytesIO(b'{"data":[{"is_sent":true,"message_id":"receipt"}]}')]
        self.assertEqual(twitch.send_twitch_reply("stage", MESSAGE, "Hi there!"), "receipt")
        checked, sent = [call.args[0] for call in open_url.call_args_list]
        self.assertEqual(checked.full_url, "https://id.twitch.tv/oauth2/validate")
        self.assertEqual(sent.full_url, "https://api.twitch.tv/helix/chat/messages")
        self.assertEqual(sent.get_method(), "POST")
        self.assertEqual(sent.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(json.loads(sent.data), {"message": "Hi there!", "sender_id": "123",
                                                "broadcaster_id": "123", "reply_parent_message_id": MESSAGE})

    @patch.dict(os.environ, {"TWITCH_OWNER_TOKEN": "test-token"})
    @patch.object(twitch, "_request_json")
    def test_wrong_channel_or_scope_rejected_before_post(self, request):
        for identity in [dict(OWNER, login="elsewhere"), dict(OWNER, scopes=[])]:
            with self.subTest(identity=identity):
                request.reset_mock()
                request.return_value = identity
                with self.assertRaises(twitch.TwitchError):
                    twitch.send_twitch_reply("stage", MESSAGE, "hi")
                self.assertEqual(request.call_count, 1)

    @patch.dict(os.environ, {"TWITCH_OWNER_TOKEN": "test-token"})
    @patch.object(twitch, "urlopen")
    def test_uncertain_deliveries_never_retried(self, open_url):
        for issue in [TimeoutError(), HTTPError("https://api.twitch.tv", 503, "Unavailable", {}, None),
                      io.BytesIO(b"[]"), io.BytesIO(b'{"data":[]}'), io.BytesIO(b"oops")]:
            with self.subTest(issue=issue):
                open_url.reset_mock()
                open_url.side_effect = [io.BytesIO(json.dumps(OWNER).encode()), issue]
                with self.assertRaises(twitch.TwitchDeliveryUnknown):
                    twitch.send_twitch_reply("stage", MESSAGE, "hi")
                self.assertEqual(open_url.call_count, 2)

    @patch.dict(os.environ, {"TWITCH_OWNER_TOKEN": "test-token"})
    @patch.object(twitch, "_request_json")
    def test_drop_reason_and_validation(self, request):
        request.side_effect = [OWNER, {"data": [{"is_sent": False, "drop_reason": {"code": "automod_held"}}]}]
        with self.assertRaisesRegex(twitch.TwitchDeliveryUnknown, "held this reply for moderation"):
            twitch.send_twitch_reply("stage", MESSAGE, "hi")
        request.reset_mock()
        for channel, parent, body in [("bad\r\n", MESSAGE, "hi"), ("stage", "bad", "hi"),
                                      ("stage", MESSAGE, "bad\nline"), ("stage", MESSAGE, "x" * 501)]:
            with self.assertRaises(twitch.TwitchError):
                twitch.send_twitch_reply(channel, parent, body)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
