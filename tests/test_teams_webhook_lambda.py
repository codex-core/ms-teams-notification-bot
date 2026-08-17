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


class TestBuildCard(unittest.TestCase):
    def setUp(self):
        import importlib
        import lambdas.teams_webhook_lambda.handler as m
        importlib.reload(m)
        self.mod = m

    def test_card_without_mentions(self):
        card = self.mod._build_card("Title", "Hello", [])
        attachment = card["attachments"][0]["content"]
        self.assertEqual(attachment["type"], "AdaptiveCard")
        bodies = attachment["body"]
        self.assertEqual(bodies[0]["text"], "Title")
        self.assertIn("Hello", bodies[1]["text"])
        self.assertEqual(attachment["msteams"]["entities"], [])

    def test_card_with_single_mention(self):
        card = self.mod._build_card("Alert", "Check this", ["alice@example.com"])
        attachment = card["attachments"][0]["content"]
        entities = attachment["msteams"]["entities"]
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0]["type"], "mention")
        # Email is used directly as the mention id
        self.assertEqual(entities[0]["mentioned"]["id"], "alice@example.com")
        self.assertIn("<at>alice</at>", attachment["body"][1]["text"])

    def test_card_multiple_mentions(self):
        emails = ["alice@example.com", "bob@example.com"]
        card = self.mod._build_card("Hi", "msg", emails)
        entities = card["attachments"][0]["content"]["msteams"]["entities"]
        self.assertEqual(len(entities), 2)
        ids = [e["mentioned"]["id"] for e in entities]
        self.assertIn("alice@example.com", ids)
        self.assertIn("bob@example.com", ids)

    def test_card_mention_display_name_is_local_part(self):
        card = self.mod._build_card("T", "M", ["john.doe@corp.com"])
        entity = card["attachments"][0]["content"]["msteams"]["entities"][0]
        self.assertEqual(entity["mentioned"]["name"], "john.doe")
        self.assertEqual(entity["text"], "<at>john.doe</at>")

    def test_card_version_is_1_0(self):
        card = self.mod._build_card("T", "M", [])
        version = card["attachments"][0]["content"]["version"]
        self.assertEqual(version, "1.0")


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
            {"title": "T", "message": "M", "mentions": ["alice@example.com"]}
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
