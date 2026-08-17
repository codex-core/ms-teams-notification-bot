"""
Lambda: flow_bot_lambda
Triggered by an SNS topic.
Posts a message to a Microsoft Teams channel via a Power Automate Flow Bot
by calling an HTTP-triggered Power Automate workflow.

The flow is triggered via an HTTP POST to a Power Automate instant-cloud-flow
endpoint (the URL provided in the problem statement).

Required environment variables / SSM parameters:
  FLOW_TRIGGER_URL_PARAM  - SSM param name holding the Power Automate trigger URL
                            (the full URL including api-version, sp, sv, sig)

Optional SNS message payload fields:
  title    (str)  - Notification title
  message  (str)  - Notification body
  mentions (list[str]) - User email addresses to notify through the flow bot
"""

import json
import logging
import os
import urllib.request
import urllib.error

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")

_cache: dict = {}


def _get_param(name: str, decrypt: bool = False) -> str:
    if name not in _cache:
        resp = ssm.get_parameter(Name=name, WithDecryption=decrypt)
        _cache[name] = resp["Parameter"]["Value"]
    return _cache[name]


def _post_to_flow(trigger_url: str, payload: dict) -> int:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        trigger_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
        logger.info("Flow trigger response: %s", resp.status)
        return resp.status


def handler(event: dict, context) -> dict:
    """Lambda entry point."""
    flow_url_param = os.environ.get(
        "FLOW_TRIGGER_URL_PARAM", "/teams-bot/flow-trigger-url"
    )
    trigger_url = _get_param(flow_url_param)

    records = event.get("Records", [])
    if not records:
        logger.warning("No SNS records found in event")
        return {"statusCode": 400, "body": "No records"}

    for record in records:
        payload = json.loads(record["Sns"]["Message"])
        title = payload.get("title", "Notification")
        message = payload.get("message", "")
        mentions: list[str] = payload.get("mentions", [])

        flow_payload = {
            "title": title,
            "message": message,
            "mentions": mentions,
        }

        status = _post_to_flow(trigger_url, flow_payload)
        logger.info("Flow response status: %s", status)

    return {"statusCode": 200, "body": "OK"}
