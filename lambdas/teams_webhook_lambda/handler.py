"""
Lambda: teams_webhook_lambda
Triggered by an SNS topic.
Sends an Adaptive Card to a Microsoft Teams channel via an Incoming Webhook,
optionally @mentioning users by their email addresses.

The SNS message carries a *simple* notification (see schema below) and this
Lambda does all of the formatting: it builds the Adaptive Card in Python
(colours by ``type``, renders links as buttons, @mentions recipients) and posts
the fully-formed message to the channel's Incoming Webhook.

Teams supports using an email address directly as the ``mentioned.id`` value in
an Adaptive Card mention entity — no Microsoft Graph lookup is required.

Required SSM Parameter Store entries:
  /teams-bot/webhook-url  (SecureString) - Incoming Webhook URL for the target
                                           Teams channel

SNS message payload (simple notification schema):
  recipients (list[str])              - Emails to @mention (aka mentions)
  dateTime   (str | list[str])        - Optional timestamp(s)
  message    (str)                    - Body text
  links      (list[str | {title,url}])- Optional links, rendered as buttons
  type       (str)                    - info (default) | warning | error
  title      (str)                    - Optional card title
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

# Maps the notification "type" to Adaptive Card colours.
#   color -> Adaptive Card TextBlock "color"
#   style -> Adaptive Card Container "style"
_TYPE_STYLES = {
    "info": {"color": "Accent", "style": "accent"},
    "warning": {"color": "Warning", "style": "warning"},
    "error": {"color": "Attention", "style": "attention"},
}


def _get_param(name: str, decrypt: bool = False) -> str:
    if name not in _cache:
        resp = ssm.get_parameter(Name=name, WithDecryption=decrypt)
        _cache[name] = resp["Parameter"]["Value"]
    return _cache[name]


def _normalize_notification(payload: dict) -> dict:
    """Normalise a raw SNS payload into the canonical notification structure.

    Accepts the current schema (``recipients``/``dateTime``/``links``/``type``)
    and remains backward compatible with the legacy ``{title, message,
    mentions}`` shape.
    """
    recipients = payload.get("recipients")
    if recipients is None:
        recipients = payload.get("mentions", [])
    recipients = [r for r in recipients if r]

    date_time = payload.get("dateTime", [])
    if isinstance(date_time, str):
        date_time = [date_time] if date_time else []

    links = []
    for link in payload.get("links") or []:
        if isinstance(link, str):
            if link:
                links.append({"title": link, "url": link})
        elif isinstance(link, dict):
            url = link.get("url") or link.get("href") or ""
            title = link.get("title") or link.get("name") or url
            if url:
                links.append({"title": title, "url": url})

    ntype = (payload.get("type") or "info").lower()
    if ntype not in _TYPE_STYLES:
        ntype = "info"

    title = payload.get("title") or f"{ntype.capitalize()} Notification"

    return {
        "recipients": recipients,
        "dateTime": date_time,
        "message": payload.get("message", ""),
        "links": links,
        "type": ntype,
        "title": title,
    }


def _build_card_content(notification: dict) -> dict:
    """Build the Adaptive Card *content* object from a normalised notification.

    Teams accepts an email address as the ``mentioned.id`` value directly, so no
    Microsoft Graph API call is needed to resolve user identities. The display
    name is derived from the local part of the email address.
    """
    styles = _TYPE_STYLES.get(notification["type"], _TYPE_STYLES["info"])

    entities = []
    mention_tags = []
    for email in notification["recipients"]:
        display_name = email.split("@")[0]
        tag = f"<at>{display_name}</at>"
        mention_tags.append(tag)
        entities.append(
            {
                "type": "mention",
                "text": tag,
                "mentioned": {"id": email, "name": display_name},
            }
        )

    body_text = notification["message"]
    if mention_tags:
        body_text += "\n\n" + " ".join(mention_tags)

    body = [
        {
            "type": "Container",
            "style": styles["style"],
            "bleed": True,
            "items": [
                {
                    "type": "TextBlock",
                    "size": "Large",
                    "weight": "Bolder",
                    "color": styles["color"],
                    "text": notification["title"],
                    "wrap": True,
                }
            ],
        },
        {"type": "TextBlock", "text": body_text, "wrap": True},
    ]

    if notification["dateTime"]:
        body.append(
            {
                "type": "TextBlock",
                "spacing": "Small",
                "isSubtle": True,
                "size": "Small",
                "wrap": True,
                "text": "🕒 " + ", ".join(notification["dateTime"]),
            }
        )

    content = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
        "msteams": {"entities": entities},
    }

    actions = [
        {"type": "Action.OpenUrl", "title": link["title"], "url": link["url"]}
        for link in notification["links"]
    ]
    if actions:
        content["actions"] = actions

    return content


def _build_card(payload: dict) -> dict:
    """Build the full Incoming Webhook message (Adaptive Card attachment)."""
    notification = _normalize_notification(payload)
    content = _build_card_content(notification)
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": content,
            }
        ],
    }


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
        card = _build_card(payload)
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
