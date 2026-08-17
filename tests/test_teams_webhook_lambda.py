"""Unit tests for the teams_webhook_lambda handler."""

import json
import sys
import unittest
from unittest.mock import MagicMock, patch

# Stub boto3 before importing handler
sys.modules.setdefault("boto3", MagicMock())


def _make_sns_event(message: dict) -> dict:
    return {
        "Records": [
            {
                "Sns": {
                    "Message": json.dumps(message),
                }
            }
        ]
    }


def _content(card: dict) -> dict:
    return card["attachments"][0]["content"]


class TestBuildCard(unittest.TestCase):
    def setUp(self):
        import importlib
        import lambdas.teams_webhook_lambda.handler as m
        importlib.reload(m)
        self.mod = m

    def test_card_without_mentions(self):
        card = self.mod._build_card({"title": "Title", "message": "Hello"})
        content = _content(card)
        self.assertEqual(content["type"], "AdaptiveCard")
        # body[0] is the coloured title container, body[1] the message
        self.assertEqual(content["body"][0]["items"][0]["text"], "Title")
        self.assertIn("Hello", content["body"][1]["text"])
        self.assertEqual(content["msteams"]["entities"], [])

    def test_card_with_single_mention(self):
        card = self.mod._build_card(
            {"message": "Check this", "recipients": ["alice@example.com"]}
        )
        content = _content(card)
        entities = content["msteams"]["entities"]
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0]["type"], "mention")
        # Email is used directly as the mention id
        self.assertEqual(entities[0]["mentioned"]["id"], "alice@example.com")
        self.assertIn("<at>alice</at>", content["body"][1]["text"])

    def test_card_multiple_mentions(self):
        emails = ["alice@example.com", "bob@example.com"]
        card = self.mod._build_card({"message": "msg", "recipients": emails})
        entities = _content(card)["msteams"]["entities"]
        self.assertEqual(len(entities), 2)
        ids = [e["mentioned"]["id"] for e in entities]
        self.assertIn("alice@example.com", ids)
        self.assertIn("bob@example.com", ids)

    def test_card_mention_display_name_is_local_part(self):
        card = self.mod._build_card(
            {"message": "M", "recipients": ["john.doe@corp.com"]}
        )
        entity = _content(card)["msteams"]["entities"][0]
        self.assertEqual(entity["mentioned"]["name"], "john.doe")
        self.assertEqual(entity["text"], "<at>john.doe</at>")

    def test_legacy_mentions_field_supported(self):
        card = self.mod._build_card(
            {"message": "M", "mentions": ["alice@example.com"]}
        )
        entities = _content(card)["msteams"]["entities"]
        self.assertEqual(entities[0]["mentioned"]["id"], "alice@example.com")

    def test_type_controls_colour(self):
        for ntype, color, style in [
            ("info", "Accent", "accent"),
            ("warning", "Warning", "warning"),
            ("error", "Attention", "attention"),
        ]:
            card = self.mod._build_card({"message": "M", "type": ntype})
            container = _content(card)["body"][0]
            self.assertEqual(container["style"], style)
            self.assertEqual(container["items"][0]["color"], color)

    def test_default_type_is_info(self):
        card = self.mod._build_card({"message": "M"})
        self.assertEqual(_content(card)["body"][0]["style"], "accent")

    def test_invalid_type_falls_back_to_info(self):
        card = self.mod._build_card({"message": "M", "type": "bogus"})
        self.assertEqual(_content(card)["body"][0]["style"], "accent")

    def test_links_render_as_actions(self):
        card = self.mod._build_card(
            {
                "message": "M",
                "links": [
                    {"title": "Runbook", "url": "https://example.com/rb"},
                    "https://example.com/plain",
                ],
            }
        )
        actions = _content(card)["actions"]
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0]["type"], "Action.OpenUrl")
        self.assertEqual(actions[0]["title"], "Runbook")
        self.assertEqual(actions[0]["url"], "https://example.com/rb")
        self.assertEqual(actions[1]["url"], "https://example.com/plain")

    def test_no_links_means_no_actions_key(self):
        card = self.mod._build_card({"message": "M"})
        self.assertNotIn("actions", _content(card))

    def test_datetime_string_is_rendered(self):
        card = self.mod._build_card(
            {"message": "M", "dateTime": "2026-08-17T17:06:39Z"}
        )
        texts = [b.get("text", "") for b in _content(card)["body"]]
        self.assertTrue(any("2026-08-17T17:06:39Z" in t for t in texts))

    def test_default_title_from_type(self):
        card = self.mod._build_card({"message": "M", "type": "error"})
        self.assertEqual(_content(card)["body"][0]["items"][0]["text"], "Error Notification")


class TestHandler(unittest.TestCase):
    def setUp(self):
        import importlib
        import lambdas.teams_webhook_lambda.handler as m
        importlib.reload(m)
        self.mod = m
        self.mod._cache.clear()

    @patch("lambdas.teams_webhook_lambda.handler.urllib.request.urlopen")
    @patch("lambdas.teams_webhook_lambda.handler._get_param")
    def test_handler_no_mentions(self, mock_get_param, mock_urlopen):
        mock_get_param.return_value = "https://fake-webhook.example.com"

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        event = _make_sns_event({"title": "T", "message": "M"})
        result = self.mod.handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        # Only one HTTP call: the webhook POST (no Graph calls)
        self.assertEqual(mock_urlopen.call_count, 1)

    def test_handler_no_records(self):
        result = self.mod.handler({"Records": []}, None)
        self.assertEqual(result["statusCode"], 400)

    @patch("lambdas.teams_webhook_lambda.handler.urllib.request.urlopen")
    @patch("lambdas.teams_webhook_lambda.handler._get_param")
    def test_handler_with_mentions_no_graph_call(self, mock_get_param, mock_urlopen):
        """Mentions are resolved from email directly — no Graph API calls."""
        mock_get_param.return_value = "https://webhook.example.com"

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        event = _make_sns_event(
            {"message": "M", "recipients": ["alice@example.com"]}
        )
        result = self.mod.handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        # Exactly one HTTP call: the webhook POST only
        self.assertEqual(mock_urlopen.call_count, 1)

        posted_card = json.loads(mock_urlopen.call_args[0][0].data)
        entities = posted_card["attachments"][0]["content"]["msteams"]["entities"]
        self.assertEqual(entities[0]["mentioned"]["id"], "alice@example.com")


if __name__ == "__main__":
    unittest.main()
