"""
Lambda: teams_webhook_lambda
Triggered by an SNS topic.
Sends an Adaptive Card to a Microsoft Teams channel via an Incoming Webhook,
optionally @mentioning users by their email addresses.

Teams supports using an email address directly as the `mentioned.id` value in
an Adaptive Card mention entity — no Microsoft Graph lookup is required.

Required SSM Parameter Store entries:
  /teams-bot/webhook-url  (SecureString) - Incoming Webhook URL for the target
                                           Teams channel (or a Power Automate
                                           HTTP-trigger URL)

Optional SNS message payload fields:
  title    (str)       - Card title
  message  (str)       - Card body text
  mentions (list[str]) - List of user email addresses to @mention
"""

import json
import logging
import os
import urllib.request

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


def _build_card(title: str, message: str, emails: list[str]) -> dict:
    """Build an Adaptive Card payload with optional @mentions.

    Teams accepts an email address as the ``mentioned.id`` value directly,
    so no Microsoft Graph API call is needed to resolve user identities.
    The display name is derived from the local part of the email address and
    used both in the ``<at>`` tag and in the ``mentioned.name`` field.
    """
    entities = []
    mention_tags = []

    for email in emails:
        display_name = email.split("@")[0]
        tag = f"<at>{display_name}</at>"
        mention_tags.append(tag)
        entities.append(
            {
                "type": "mention",
                "text": tag,
                "mentioned": {
                    "id": email,
                    "name": display_name,
                },
            }
        )

    body_text = message
    if mention_tags:
        body_text += "  \n" + " ".join(mention_tags)

    card = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.0",
                    "body": [
                        {
                            "type": "TextBlock",
                            "size": "Medium",
                            "weight": "Bolder",
                            "text": title,
                        },
                        {
                            "type": "TextBlock",
                            "text": body_text,
                            "wrap": True,
                        },
                    ],
                    "msteams": {"entities": entities},
                },
            }
        ],
    }
    return card


def handler(event: dict, context) -> dict:
    """Lambda entry point."""
    webhook_url = _get_param(
        os.environ.get("TEAMS_WEBHOOK_URL_PARAM", "/teams-bot/webhook-url")
    )

    records = event.get("Records", [])
    if not records:
        logger.warning("No SNS records found in event")
        return {"statusCode": 400, "body": "No records"}

    for record in records:
        payload = json.loads(record["Sns"]["Message"])
        title = payload.get("title", "Notification")
        message = payload.get("message", "")
        emails: list[str] = payload.get("mentions", [])

        card = _build_card(title, message, emails)
        data = json.dumps(card).encode()
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            logger.info("Teams webhook response: %s", resp.status)

    return {"statusCode": 200, "body": "OK"}
