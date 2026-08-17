"""
Lambda: teams_webhook_lambda
Triggered by an SNS topic.
Sends an Adaptive Card to a Microsoft Teams channel via an Incoming Webhook,
optionally @mentioning users by their email addresses.

Required environment variables (stored in SSM Parameter Store):
  TEAMS_WEBHOOK_URL   - Incoming Webhook URL for the target Teams channel
  GRAPH_TENANT_ID     - Azure AD tenant ID (for MS Graph token)
  GRAPH_CLIENT_ID     - Azure AD app registration client ID
  GRAPH_CLIENT_SECRET - Azure AD app registration client secret (SSM SecureString)

Optional SNS message payload fields:
  title   (str)  - Card title
  message (str)  - Card body text
  mentions (list[str]) - List of user email addresses to @mention
"""

import json
import logging
import os
import urllib.request
import urllib.parse
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


def _get_graph_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
        }
    ).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read())["access_token"]


def _lookup_user(email: str, token: str) -> dict | None:
    """Return {id, displayName} for a user by email via MS Graph."""
    encoded = urllib.parse.quote(email)
    url = f"https://graph.microsoft.com/v1.0/users/{encoded}?$select=id,displayName"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            data = json.loads(resp.read())
            return {"id": data["id"], "displayName": data["displayName"]}
    except urllib.error.HTTPError as exc:
        logger.warning("Graph lookup failed for %s: %s", email, exc)
        return None


def _build_card(title: str, message: str, mention_users: list[dict]) -> dict:
    """Build an Adaptive Card payload with optional @mentions."""
    entities = []
    mention_texts = []

    for user in mention_users:
        tag = f"<at>{user['displayName']}</at>"
        mention_texts.append(tag)
        entities.append(
            {
                "type": "mention",
                "text": tag,
                "mentioned": {
                    "id": user["id"],
                    "name": user["displayName"],
                },
            }
        )

    body_text = message
    if mention_texts:
        body_text += "  \n" + " ".join(mention_texts)

    card = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
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

        mention_users: list[dict] = []
        if emails:
            tenant_id = _get_param(
                os.environ.get("GRAPH_TENANT_ID_PARAM", "/teams-bot/graph-tenant-id")
            )
            client_id = _get_param(
                os.environ.get("GRAPH_CLIENT_ID_PARAM", "/teams-bot/graph-client-id")
            )
            client_secret = _get_param(
                os.environ.get(
                    "GRAPH_CLIENT_SECRET_PARAM", "/teams-bot/graph-client-secret"
                ),
                decrypt=True,
            )
            token = _get_graph_token(tenant_id, client_id, client_secret)
            for email in emails:
                user = _lookup_user(email, token)
                if user:
                    mention_users.append(user)

        card = _build_card(title, message, mention_users)
        data = json.dumps(card).encode()
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            status = resp.status
            logger.info("Teams webhook response: %s", status)

    return {"statusCode": 200, "body": "OK"}
