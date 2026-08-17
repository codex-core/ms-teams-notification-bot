"""
Lambda: flow_bot_lambda
Triggered by an SNS topic.
Posts a message to Microsoft Teams via a Power Automate Flow Bot.

The SNS message carries a *simple* notification (see schema below). This Lambda
does all of the formatting — it builds the Teams **Adaptive Card** in Python and
sends the Power Automate flow a ready-to-use payload, so the flow itself only has
to map two fields:

    Recipient      <-  triggerBody()?['recipient']
    Adaptive Card  <-  triggerBody()?['card']

One HTTP POST is made per recipient (the "Post card in a chat with Flow bot"
action takes a single recipient), so the flow stays trivial.

Required environment variables / SSM parameters:
  FLOW_TRIGGER_URL_PARAM  - SSM param name holding the Power Automate trigger URL

SNS message payload (simple notification schema):
  recipients (list[str])              - Emails to send / @mention (aka mentions)
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
import urllib.error

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
    """Build the Adaptive Card *content* object from a normalised notification."""
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
        notification = _normalize_notification(payload)
        card = _build_card_content(notification)

        # One request per recipient — the flow's "Post card in a chat with Flow
        # bot" action targets a single recipient. When no recipients are given,
        # send a single request with an empty recipient (e.g. for channel posts).
        recipients = notification["recipients"] or [""]
        for recipient in recipients:
            flow_payload = {"recipient": recipient, "card": card}
            status = _post_to_flow(trigger_url, flow_payload)
            logger.info("Flow response status: %s (recipient=%s)", status, recipient)

    return {"statusCode": 200, "body": "OK"}
