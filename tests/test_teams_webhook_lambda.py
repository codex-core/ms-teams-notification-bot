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
        # Re-import fresh for each test
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

    def test_card_with_mentions(self):
        users = [{"id": "aad-id-1", "displayName": "Alice"}]
        card = self.mod._build_card("Alert", "Check this", users)
        attachment = card["attachments"][0]["content"]
        entities = attachment["msteams"]["entities"]
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0]["type"], "mention")
        self.assertEqual(entities[0]["mentioned"]["id"], "aad-id-1")
        self.assertIn("<at>Alice</at>", attachment["body"][1]["text"])

    def test_card_multiple_mentions(self):
        users = [
            {"id": "id-1", "displayName": "Alice"},
            {"id": "id-2", "displayName": "Bob"},
        ]
        card = self.mod._build_card("Hi", "msg", users)
        entities = card["attachments"][0]["content"]["msteams"]["entities"]
        self.assertEqual(len(entities), 2)


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

    def test_handler_no_records(self):
        result = self.mod.handler({"Records": []}, None)
        self.assertEqual(result["statusCode"], 400)

    @patch("lambdas.teams_webhook_lambda.handler.urllib.request.urlopen")
    @patch("lambdas.teams_webhook_lambda.handler._get_param")
    def test_handler_with_mentions(self, mock_get_param, mock_urlopen):
        def side_effect(name, decrypt=False):
            return {
                "/teams-bot/webhook-url": "https://webhook.example.com",
                "/teams-bot/graph-tenant-id": "tenant-id",
                "/teams-bot/graph-client-id": "client-id",
                "/teams-bot/graph-client-secret": "secret",
            }.get(name, "value")

        mock_get_param.side_effect = side_effect

        # Token response
        token_resp = MagicMock()
        token_resp.read.return_value = json.dumps({"access_token": "tok"}).encode()
        token_resp.__enter__ = lambda s: s
        token_resp.__exit__ = MagicMock(return_value=False)

        # Graph user response
        user_resp = MagicMock()
        user_resp.read.return_value = json.dumps(
            {"id": "aad-123", "displayName": "Alice"}
        ).encode()
        user_resp.__enter__ = lambda s: s
        user_resp.__exit__ = MagicMock(return_value=False)

        # Webhook POST response
        webhook_resp = MagicMock()
        webhook_resp.status = 200
        webhook_resp.__enter__ = lambda s: s
        webhook_resp.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [token_resp, user_resp, webhook_resp]

        event = _make_sns_event(
            {"title": "T", "message": "M", "mentions": ["alice@example.com"]}
        )
        result = self.mod.handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(mock_urlopen.call_count, 3)


if __name__ == "__main__":
    unittest.main()
