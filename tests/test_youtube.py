import json
import unittest
from unittest.mock import call, patch
from urllib.parse import parse_qs, urlsplit

from socialbot import youtube as yt


def message(identifier, method="textMessageEvent", **details):
    return {"id": identifier, "authorDetails": {"displayName": "River", "channelId": "UCriver"},
            "snippet": {"type": method, "liveChatId": "live-broadcast",
                        "textMessageDetails": {"messageText": "hello"}, **details}}


def page(*items, cursor="a&b", wait=2500, **details):
    return {"items": list(items), "nextPageToken": cursor, "pollingIntervalMillis": wait, **details}


def read(**options):
    return list(yt.iter_events("studio", "live-broadcast", "abc-key", **options))


class YouTubeTests(unittest.TestCase):
    def test_skip_history_follow_cursor_millis_and_normalize_explicit_types(self):
        recent = [message("history"), message("chat"), message("paid", "superChatEvent",
                  superChatDetails={"amountDisplayString": "$2.00"}),
                  message("sticker", "superStickerEvent", superStickerDetails={}),
                  message("member", "newSponsorEvent")]
        with patch.object(yt, "_get_page", side_effect=[page(message("history")), page(*recent)]) as get:
            with patch.object(yt.time, "sleep") as sleep:
                result = read(once=True)
        self.assertEqual([e["kind"] for e in result], ["chat", "gift", "gift"])
        self.assertEqual([e["id"] for e in result], ["chat", "paid", "sticker"])
        self.assertEqual(result[0], {"platform": "youtube", "channel": "studio", "id": "chat",
                                    "kind": "chat", "user": "River", "user_id": "UCriver", "text": "hello"})
        self.assertEqual(result[1]["text"], "Super Chat ($2.00)")
        self.assertEqual(result[2]["text"], "Super Sticker")
        self.assertEqual(get.call_args_list, [call("live-broadcast", "abc-key", None),
                                              call("live-broadcast", "abc-key", "a&b")])
        sleep.assert_called_once_with(2.5)

    def test_owner_foreign_chat_and_unidentified_objects_skipped(self):
        owner = message("owner")
        owner["authorDetails"]["isChatOwner"] = True
        unidentified = message("unknown")
        del unidentified["authorDetails"]["channelId"]
        with patch.object(yt, "_get_page", side_effect=[page(), page(owner, unidentified, None,
                message("", "textMessageEvent"), message("x", liveChatId="another"), offlineAt="2026-01-01T00:00:00Z")]):
            with patch.object(yt.time, "sleep"):
                self.assertEqual(read(), [])

    def test_final_page_delivered_before_stop_without_followup(self):
        for end_signal in [page(message("last"), offlineAt="2026-01-01T00:00:00Z"),
                           page(message("last"), message("ended", "chatEndedEvent"))]:
            with self.subTest(end=end_signal), patch.object(yt, "_get_page", side_effect=[page(), end_signal]) as get:
                with patch.object(yt.time, "sleep") as sleep:
                    self.assertEqual([e["id"] for e in read()], ["last"])
                    self.assertEqual(get.call_count, 2)
                    sleep.assert_called_once()

    def test_no_busy_poll_on_invalid_continuation(self):
        for invalid in [page(cursor=None), page(wait=-1), page(wait=False)]:
            with self.subTest(invalid=invalid), patch.object(yt, "_get_page", return_value=invalid) as get:
                with self.assertRaises(yt.FeedError):
                    read()
                get.assert_called_once()

    def test_fixed_host_secret_header_and_opaque_cursor_encoding(self):
        with patch.object(yt.http.client, "HTTPSConnection") as connect:
            response = connect.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = json.dumps(page()).encode()
            yt._get_page("live&chat", "abc-key", "a/b+c&d")
            connect.assert_called_once_with("www.googleapis.com", timeout=30)
            req = connect.return_value.request.call_args
            self.assertEqual(req.args[0], "GET")
            self.assertEqual(req.kwargs["headers"]["X-Goog-Api-Key"], "abc-key")
            url = urlsplit(req.args[1])
            self.assertNotIn("abc-key", req.args[1])
            self.assertEqual(url.path, "/youtube/v3/liveChat/messages")
            self.assertEqual(parse_qs(url.query)["pageToken"], ["a/b+c&d"])
            self.assertEqual(parse_qs(url.query)["liveChatId"], ["live&chat"])
            connect.return_value.close.assert_called_once()

    def test_no_redirect_and_no_credentials_in_errors(self):
        with patch.object(yt.http.client, "HTTPSConnection") as connect:
            connect.return_value.getresponse.return_value.status = 302
            with self.assertRaisesRegex(yt.FeedError, "HTTP 302") as ctx:
                read()
            self.assertNotIn("abc-key", str(ctx.exception))
            connect.assert_called_once()
            connect.return_value.getresponse.return_value.read.assert_not_called()
