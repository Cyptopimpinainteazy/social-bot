import json
import unittest
from unittest.mock import patch

from socialbot import chaturbate as cb


ROOM = "studio"
TOKEN = "test-key-123"
BASE = "https://eventsapi.chaturbate.com/events/studio/test-key-123/"


def event(method, event_id, **details):
    obj = {"broadcaster": ROOM, "user": {"username": "River"}, **details}
    return {"method": method, "id": event_id, "object": obj}


class EventsTests(unittest.TestCase):
    def test_public_events_keep_upstream_ids_and_minimal_fields(self):
        page = {"events": [
            event("chatMessage", "10-0", message={"message": "hello"}),
            event("userEnter", "10-1"),
            event("follow", "10-2"),
            event("tip", "10-3", tip={"tokens": 8, "isAnon": False, "message": "tip note"}),
            event("tip", "10-4", tip={"tokens": 8, "isAnon": False}),
        ], "nextUrl": BASE + "?i=10-4&timeout=10"}
        with patch.object(cb, "_get_page", return_value=page):
            result = list(cb.iter_events(ROOM.upper(), TOKEN, once=True))
        self.assertEqual([r["id"] for r in result], [f"10-{n}" for n in range(5)])
        self.assertEqual([r["kind"] for r in result], ["chat", "join", "follow", "gift", "gift"])
        self.assertEqual(result[-1], {"id": "10-4", "platform": "chaturbate", "channel": ROOM,
                                      "kind": "gift", "user": "River", "text": "8 tokens"})
        self.assertEqual(result[0]["text"], "hello")
        self.assertNotIn("tip note", repr(result))

    def test_private_anonymous_foreign_room_self_and_malformed_events_dropped(self):
        events = [
            event("privateMessage", "1", message={"message": "private"}),
            event("tip", "2", tip={"tokens": 1, "isAnon": True}),
            event("tip", "3", tip={"tokens": 1}),
            event("tip", "4", tip={"tokens": True, "isAnon": False}),
            event("userEnter", "5", broadcaster="another_room"),
            event("userEnter", "6", user={"username": "StuDio"}),
            event("userEnter", ""),
            event("userEnter", "7", user=None),
            event("chatMessage", "8", message={"message": None}),
            event("unfollow", "9"),
            {"method": {}},
            None,
        ]
        with patch.object(cb, "_get_page", return_value={"events": events, "nextUrl": BASE}):
            self.assertEqual(list(cb.iter_events(ROOM, TOKEN, once=True)), [])

    def test_follow_cursor_across_empty_and_busy_pages(self):
        with patch.object(cb, "_get_page", side_effect=[
            {"events": [], "nextUrl": BASE + "?i=10-0&timeout=10"},
            {"events": [event("userEnter", "11-0")], "nextUrl": BASE + "?i=11-0"},
        ]) as fetch:
            result = cb.iter_events(ROOM, TOKEN)
            self.assertEqual(next(result)["id"], "11-0")
            result.close()
        self.assertEqual([args.args[0] for args in fetch.call_args_list], [
            "/events/studio/test-key-123/?timeout=10", "/events/studio/test-key-123/?i=10-0&timeout=10"
        ])

    def test_cursor_cannot_change_destination(self):
        for cursor in [BASE.replace("https:", "http:"), BASE.replace("eventsapi.", "evil."),
                       BASE.replace("studio", "other"), BASE.replace(TOKEN, "other"),
                       BASE.replace(".com", ".com:1234"), BASE + "#fragment",
                       BASE.replace("https://", "https://attacker@"), BASE + "\n"]:
            with self.subTest(cursor=cursor), patch.object(
                cb, "_get_page", return_value={"events": [], "nextUrl": cursor}
            ) as fetch, self.assertRaises(cb.FeedError) as ctx:
                list(cb.iter_events(ROOM, TOKEN))
            self.assertNotIn(TOKEN, str(ctx.exception))
            fetch.assert_called_once()

    def test_redirects_rejected_without_reading_body_or_exposing_url(self):
        with patch.object(cb.http.client, "HTTPSConnection") as connect:
            response = connect.return_value.getresponse.return_value
            response.status = 302
            with self.assertRaisesRegex(cb.FeedError, "HTTP 302") as ctx:
                list(cb.iter_events(ROOM, TOKEN, once=True))
            self.assertNotIn(TOKEN, str(ctx.exception))
            response.read.assert_not_called()
            connect.assert_called_once()

    def test_invalid_channel_or_token_fails_before_io(self):
        with patch.object(cb, "_get_page") as fetch:
            for channel, token in [("other/room", TOKEN), (ROOM, "a?b"), (ROOM, ""), (ROOM, "..")]:
                with self.subTest(channel=channel, token=token), self.assertRaises(cb.FeedError):
                    list(cb.iter_events(channel, token, once=True))
            fetch.assert_not_called()

    def test_network_retries_same_page_and_closes_connections(self):
        with patch.object(cb.time, "sleep") as sleep, patch.object(
            cb.http.client, "HTTPSConnection"
        ) as connect:
            client = connect.return_value
            client.request.side_effect = [OSError("request failed " + TOKEN), None]
            response = client.getresponse.return_value
            response.status = 200
            response.read.return_value = json.dumps({"events": [], "nextUrl": BASE}).encode()
            self.assertEqual(list(cb.iter_events(ROOM, TOKEN, once=True)), [])
            self.assertEqual(client.request.call_args_list[0], client.request.call_args_list[1])
            self.assertEqual(client.close.call_count, 2)
            sleep.assert_called_once_with(1)
