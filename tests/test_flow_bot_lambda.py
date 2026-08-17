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
    def test_handler_posts_to_flow(self, mock_get_param, mock_urlopen):
        mock_get_param.return_value = "https://flow.example.com/trigger"

        mock_resp = MagicMock()
        mock_resp.status = 202
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        event = _make_sns_event(
            {"title": "Alert", "message": "Something happened", "mentions": ["bob@example.com"]}
        )
        result = self.mod.handler(event, None)
        self.assertEqual(result["statusCode"], 200)

        call_args = mock_urlopen.call_args[0][0]
        posted = json.loads(call_args.data)
        self.assertEqual(posted["title"], "Alert")
        self.assertEqual(posted["message"], "Something happened")
        self.assertEqual(posted["mentions"], ["bob@example.com"])

    @patch("lambdas.flow_bot_lambda.handler.urllib.request.urlopen")
    @patch("lambdas.flow_bot_lambda.handler._get_param")
    def test_handler_multiple_records(self, mock_get_param, mock_urlopen):
        mock_get_param.return_value = "https://flow.example.com/trigger"

        mock_resp = MagicMock()
        mock_resp.status = 202
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        event = {
            "Records": [
                {"Sns": {"Message": json.dumps({"title": "T1", "message": "M1"})}},
                {"Sns": {"Message": json.dumps({"title": "T2", "message": "M2"})}},
            ]
        }
        result = self.mod.handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(mock_urlopen.call_count, 2)


if __name__ == "__main__":
    unittest.main()
