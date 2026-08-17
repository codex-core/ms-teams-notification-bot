"""Unit tests for the flow_bot_lambda handler."""

import json
import sys
import unittest
from unittest.mock import MagicMock, patch

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


def _mock_resp(status=202):
    resp = MagicMock()
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestBuildCardContent(unittest.TestCase):
    def setUp(self):
        import importlib
        import lambdas.flow_bot_lambda.handler as m
        importlib.reload(m)
        self.mod = m

    def test_type_controls_colour(self):
        for ntype, color, style in [
            ("info", "Accent", "accent"),
            ("warning", "Warning", "warning"),
            ("error", "Attention", "attention"),
        ]:
            note = self.mod._normalize_notification({"message": "M", "type": ntype})
            content = self.mod._build_card_content(note)
            self.assertEqual(content["body"][0]["style"], style)
            self.assertEqual(content["body"][0]["items"][0]["color"], color)

    def test_mentions_and_links(self):
        note = self.mod._normalize_notification(
            {
                "message": "M",
                "recipients": ["alice@example.com"],
                "links": [{"title": "Docs", "url": "https://x/docs"}],
            }
        )
        content = self.mod._build_card_content(note)
        self.assertEqual(
            content["msteams"]["entities"][0]["mentioned"]["id"], "alice@example.com"
        )
        self.assertEqual(content["actions"][0]["url"], "https://x/docs")

    def test_legacy_mentions_supported(self):
        note = self.mod._normalize_notification(
            {"message": "M", "mentions": ["bob@example.com"]}
        )
        self.assertEqual(note["recipients"], ["bob@example.com"])


class TestFlowBotHandler(unittest.TestCase):
    def setUp(self):
        import importlib
        import lambdas.flow_bot_lambda.handler as m
        importlib.reload(m)
        self.mod = m
        self.mod._cache.clear()

    def test_handler_no_records(self):
        result = self.mod.handler({"Records": []}, None)
        self.assertEqual(result["statusCode"], 400)

    @patch("lambdas.flow_bot_lambda.handler.urllib.request.urlopen")
    @patch("lambdas.flow_bot_lambda.handler._get_param")
    def test_handler_posts_card_and_recipient(self, mock_get_param, mock_urlopen):
        mock_get_param.return_value = "https://flow.example.com/trigger"
        mock_urlopen.return_value = _mock_resp()

        event = _make_sns_event(
            {
                "message": "Something happened",
                "recipients": ["bob@example.com"],
                "type": "warning",
            }
        )
        result = self.mod.handler(event, None)
        self.assertEqual(result["statusCode"], 200)

        posted = json.loads(mock_urlopen.call_args[0][0].data)
        # The flow now receives a ready-made card + a single recipient.
        self.assertEqual(posted["recipient"], "bob@example.com")
        self.assertEqual(posted["card"]["type"], "AdaptiveCard")
        self.assertEqual(posted["card"]["body"][0]["style"], "warning")
        self.assertIn("Something happened", posted["card"]["body"][1]["text"])

    @patch("lambdas.flow_bot_lambda.handler.urllib.request.urlopen")
    @patch("lambdas.flow_bot_lambda.handler._get_param")
    def test_handler_one_request_per_recipient(self, mock_get_param, mock_urlopen):
        mock_get_param.return_value = "https://flow.example.com/trigger"
        mock_urlopen.return_value = _mock_resp()

        event = _make_sns_event(
            {"message": "M", "recipients": ["a@x.com", "b@x.com", "c@x.com"]}
        )
        self.mod.handler(event, None)
        self.assertEqual(mock_urlopen.call_count, 3)
        recipients = [
            json.loads(c[0][0].data)["recipient"] for c in mock_urlopen.call_args_list
        ]
        self.assertEqual(recipients, ["a@x.com", "b@x.com", "c@x.com"])

    @patch("lambdas.flow_bot_lambda.handler.urllib.request.urlopen")
    @patch("lambdas.flow_bot_lambda.handler._get_param")
    def test_handler_no_recipients_sends_once(self, mock_get_param, mock_urlopen):
        mock_get_param.return_value = "https://flow.example.com/trigger"
        mock_urlopen.return_value = _mock_resp()

        event = _make_sns_event({"message": "M"})
        self.mod.handler(event, None)
        self.assertEqual(mock_urlopen.call_count, 1)
        self.assertEqual(json.loads(mock_urlopen.call_args[0][0].data)["recipient"], "")

    @patch("lambdas.flow_bot_lambda.handler.urllib.request.urlopen")
    @patch("lambdas.flow_bot_lambda.handler._get_param")
    def test_handler_multiple_records(self, mock_get_param, mock_urlopen):
        mock_get_param.return_value = "https://flow.example.com/trigger"
        mock_urlopen.return_value = _mock_resp()

        event = {
            "Records": [
                {"Sns": {"Message": json.dumps({"message": "M1", "recipients": ["a@x.com"]})}},
                {"Sns": {"Message": json.dumps({"message": "M2", "recipients": ["b@x.com"]})}},
            ]
        }
        result = self.mod.handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(mock_urlopen.call_count, 2)


if __name__ == "__main__":
    unittest.main()
