# ms-teams-notification-bot

An AWS Lambda-based notification system that posts messages to Microsoft Teams
via two mechanisms:

1. **Teams Incoming Webhook** — sends an Adaptive Card with optional `@mention`
   support using email addresses directly (no Microsoft Graph API required).
2. **Power Automate Flow Bot** — triggers a Power Automate instant cloud flow
   that delivers messages through the Teams Flow Bot.

Both Lambdas are triggered by the same **SNS topic**, and all secrets are stored
in **AWS SSM Parameter Store** as `SecureString` values.

---

## Architecture

```
SNS Topic (teams-notifications-<env>)
        │
        ├──► Lambda: teams-webhook-notification-<env>
        │          │
        │          └── SSM: /teams-bot/webhook-url          (Teams Incoming Webhook URL)
        │
        └──► Lambda: teams-flow-bot-notification-<env>
                   │
                   └── SSM: /teams-bot/flow-trigger-url     (Power Automate trigger URL)
```

---

## SNS Message Payload

Publish a **simple** JSON message to the SNS topic — both Lambdas do all Teams
formatting (Adaptive Card, colours, `@mentions`, link buttons) in Python, so
producers only need to send plain data:

```json
{
  "recipients": ["alice@example.com", "bob@example.com"],
  "dateTime": ["2026-08-17T17:06:39Z"],
  "message": "The v2.1.0 release has been deployed to production.",
  "links": [{ "title": "Release notes", "url": "https://example.com/releases" }],
  "type": "info"
}
```

| Field        | Type                     | Required | Description                                                          |
|--------------|--------------------------|----------|----------------------------------------------------------------------|
| `recipients` | list\[string\]           | No       | Email addresses of users to `@mention` / send to                     |
| `dateTime`   | string \| list\[string\] | No       | Timestamp(s) shown on the card                                       |
| `message`    | string                   | No       | Main body text                                                       |
| `links`      | list\[string \| object\] | No       | `"https://url"` or `{ "title": …, "url": … }`; rendered as buttons   |
| `type`       | string                   | No       | `info` (default), `warning`, or `error` — controls the card colour   |
| `title`      | string                   | No       | Optional card title (defaults to `"<Type> Notification"`)            |

> **Backward compatible:** the legacy `{ "title", "message", "mentions" }` shape
> is still accepted — `mentions` is treated as `recipients`.

`type` maps to an Adaptive Card colour:

| `type`    | Card colour / container style |
|-----------|-------------------------------|
| `info`    | `accent` (blue)               |
| `warning` | `warning` (amber)             |
| `error`   | `attention` (red)             |

---

## Lambda 1 — Teams Incoming Webhook (`@mention` via email)

### How it works

1. Receives SNS record and parses the simple notification payload.
2. For each email address in `recipients`, derives the display name from the
   local part (e.g. `alice@example.com` → `alice`) and builds a Teams mention
   entity with `mentioned.id` set to the email address directly — Teams accepts
   an email address as a valid user identifier in Adaptive Card mention entities,
   so **no Microsoft Graph API call is required**.
3. Constructs an **Adaptive Card** — colour-coded by `type`, with a title
   container, the message body, optional `dateTime`, `links` rendered as buttons,
   and `msteams.entities` entries so Teams renders the familiar `@alice` pill.
4. POSTs the card to the channel's **Incoming Webhook** URL.

### Teams Configuration — Incoming Webhook

1. Open the target Teams channel → **…** menu → **Connectors** → **Incoming Webhook** → **Configure**.
2. Give it a name (e.g. `AWS Notifications`) and optionally upload an icon.
3. Click **Create** and copy the generated webhook URL.
4. Store the URL as `var.teams_webhook_url` when running Terraform (it is saved
   to SSM as a `SecureString`).

---

## Lambda 2 — Power Automate Flow Bot

### How it works

1. Receives the SNS record and parses the simple notification payload.
2. **Builds the Adaptive Card in Python** (same formatting as the webhook
   Lambda — colour by `type`, `@mentions`, `dateTime`, `links`).
3. For **each** recipient, POSTs `{ "recipient": "<email>", "card": { … } }` to
   the Power Automate HTTP trigger URL stored in SSM (one request per recipient,
   since the Flow Bot chat action targets a single recipient). When there are no
   recipients, a single request is sent with an empty `recipient`.
4. The flow only has to map two fields — `Recipient` and `Adaptive Card` — from
   the trigger body, so **no formatting logic lives in Power Automate**.

### Power Automate Flow Configuration

> **Required Teams license:** The Teams Flow Bot connector requires a **Power
> Automate per-user or per-flow plan** (or a Microsoft 365 plan that includes
> Power Automate). The Flow Bot is a special service account that posts on
> behalf of the flow — users receive the message as a bot chat or channel post.

Follow these steps to set up the flow:

#### Step 1 — Create an Instant Cloud Flow with HTTP trigger

1. Go to [make.powerautomate.com](https://make.powerautomate.com) and sign in.
2. Click **+ Create → Instant cloud flow**.
3. Choose **"When an HTTP request is received"** as the trigger and click
   **Create**.
4. In the trigger card, set **Request Body JSON Schema** (the Lambda posts a
   ready-made card, so the flow only needs `recipient` and `card`):

```json
{
  "type": "object",
  "properties": {
    "recipient": { "type": "string" },
    "card":      { "type": "object" }
  }
}
```

5. **Save** the flow — the trigger URL is generated and shown in the trigger card.
   Copy this URL and store it as `var.flow_trigger_url` in Terraform.

#### Step 2 — Add the "Post card in a chat or channel" action

1. Click **+ New step**.
2. Search for **Microsoft Teams** and choose **"Post card in a chat or channel"**.
3. Configure (matching the fields flagged in the Workflows screenshot):
   - **Post as**: `Flow bot`
   - **Post in**: `Chat with Flow bot`
   - **Recipient**: dynamic content → `triggerBody()?['recipient']`
   - **Adaptive Card**: dynamic content → `triggerBody()?['card']`

That's it — the Lambda already built the coloured, `@mention`-rich Adaptive Card,
so there is nothing else to configure. The Lambda sends one request per
recipient, so each recipient receives their own 1-on-1 card from the Flow Bot.

> **@mentions:** the card's `msteams.entities` are populated by the Lambda using
> the recipient email addresses directly — **no Microsoft Graph lookup** and no
> extra Azure AD permissions are required.

#### Step 3 — Secure the HTTP trigger (optional but recommended)

Power Automate HTTP triggers include a `sig` query parameter for HMAC
verification. Additionally:

- **IP restriction**: In the flow trigger settings, set "Who can trigger this
  flow" to **Specific users** or add an IP allowlist in Azure API Management.
- **Secret header**: Add a "Condition" step at the start of the flow that checks
  for a shared-secret header (e.g. `X-Flow-Secret`) and terminates with a 401
  response if absent. Pass this secret as an additional SSM parameter.

---

## Infrastructure (Terraform)

### Prerequisites

- Terraform ≥ 1.5
- AWS credentials with permissions to manage Lambda, SNS, IAM, and SSM.
- An AWS region configured.

### Deploy

```bash
cd terraform/

terraform init

terraform apply \
  -var="teams_webhook_url=https://YOUR_TENANT.webhook.office.com/..." \
  -var="flow_trigger_url=https://prod-xx.westus.logic.azure.com:443/..."
```

| Variable            | Description                                                   |
|---------------------|---------------------------------------------------------------|
| `teams_webhook_url` | Teams channel Incoming Webhook URL                            |
| `flow_trigger_url`  | Power Automate HTTP trigger URL                               |
| `aws_region`        | AWS region (default: `us-east-1`)                             |
| `environment`       | Deployment environment tag (default: `dev`)                   |

### Resources created

| Resource                          | Description                                   |
|-----------------------------------|-----------------------------------------------|
| `aws_sns_topic`                   | SNS topic that fans out to both Lambdas       |
| `aws_lambda_function` ×2          | Webhook Lambda and Flow Bot Lambda            |
| `aws_iam_role`                    | Shared Lambda execution role                  |
| `aws_iam_policy` (SSM)            | Grants SSM `GetParameter` + KMS `Decrypt`     |
| `aws_ssm_parameter` ×2            | `SecureString` parameters for webhook URL and flow trigger URL |
| `aws_sns_topic_subscription` ×2   | Subscribe each Lambda to the SNS topic        |
| `aws_lambda_permission` ×2        | Allow SNS to invoke each Lambda               |

---

## Project Layout

```
.
├── lambdas/
│   ├── teams_webhook_lambda/
│   │   └── handler.py          # Incoming Webhook + @mention via email (no Graph)
│   └── flow_bot_lambda/
│       └── handler.py          # Power Automate HTTP trigger
├── terraform/
│   ├── main.tf                 # Provider & backend config
│   ├── variables.tf            # Input variables
│   ├── resources.tf            # All AWS resources
│   └── outputs.tf              # ARN outputs
├── tests/
│   ├── test_teams_webhook_lambda.py
│   └── test_flow_bot_lambda.py
└── README.md
```

---

## Test Harness — `scripts/send-notification.sh`

`scripts/send-notification.sh` forwards a **simple** notification to the SNS
topic (or directly to an HTTP trigger) so you can exercise the Lambdas
end-to-end. All Teams formatting happens in the Python Lambdas, so the script
just sends the plain notification JSON — no card building.

### Notification schema

```json
{
  "recipients": ["alice@example.com", "bob@example.com"],
  "dateTime":   ["2026-08-17T17:06:39Z"],
  "message":    "The v2.1.0 release has been deployed to production.",
  "links":      [{ "title": "Runbook", "url": "https://example.com/runbook" }],
  "type":       "info"
}
```

| Field        | Type                 | Required | Description                                                        |
|--------------|----------------------|----------|--------------------------------------------------------------------|
| `recipients` | list\[string\]       | No       | Emails to `@mention` / send to                                     |
| `dateTime`   | list\[string\]       | No       | Timestamp(s); defaults to the current UTC time when built by flags |
| `message`    | string               | Yes      | Main body text                                                     |
| `links`      | list\[string\|object\] | No     | `"https://url"` or `{ "title": …, "url": … }`; rendered as buttons |
| `type`       | string               | No       | `info` (default), `warning`, or `error`                            |

`type` is forwarded as-is; the Lambdas map it to an Adaptive Card colour:

| `type`    | Card colour / container style |
|-----------|-------------------------------|
| `info`    | `accent` (blue)               |
| `warning` | `warning` (amber)             |
| `error`   | `attention` (red)             |

### What it sends

Every target receives the **same simple notification JSON** shown above; the
Python Lambdas turn it into a formatted Teams Adaptive Card downstream.

| Target                   | Flag        | Payload posted                                              |
|--------------------------|-------------|-------------------------------------------------------------|
| SNS topic (Lambdas)      | `--sns`     | The notification JSON, published to the SNS topic (recommended) |
| Power Automate / HTTP    | `--flow`    | The notification JSON, POSTed to the HTTP trigger           |
| Webhook / HTTP endpoint  | `--webhook` | The notification JSON, POSTed to the URL                    |

Requirements: `bash`, `jq`, `curl` (and the AWS CLI for `--sns`).

### Usage

```bash
# Preview the notification JSON without sending (dry run)
scripts/send-notification.sh -m "Deploy finished" -r alice@example.com -t info --dry-run

# Publish a simple message to SNS (drives the real Lambdas)
scripts/send-notification.sh \
  -m "Disk almost full" -r ops@example.com -t warning \
  -l "Runbook=https://wiki/df" \
  --sns arn:aws:sns:us-east-1:123456789012:teams-notifications-dev

# Read a prepared file and publish to SNS end-to-end
scripts/send-notification.sh --file scripts/examples/notification.json \
  --sns arn:aws:sns:us-east-1:123456789012:teams-notifications-dev
```

Target URLs/ARNs can also come from the `WEBHOOK_URL`, `FLOW_TRIGGER_URL`, and
`SNS_TOPIC_ARN` environment variables (use `--all` to send to all three). Run
`scripts/send-notification.sh --help` for the full option list.

---

## Running Tests

```bash
pip install pytest
python -m pytest tests/ -v
```

