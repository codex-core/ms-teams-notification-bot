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

Publish a JSON message to the SNS topic:

```json
{
  "title": "Deployment Complete",
  "message": "The v2.1.0 release has been deployed to production.",
  "mentions": [
    "alice@example.com",
    "bob@example.com"
  ]
}
```

| Field      | Type           | Required | Description                                         |
|------------|----------------|----------|-----------------------------------------------------|
| `title`    | string         | No       | Card/message title (defaults to `"Notification"`)   |
| `message`  | string         | No       | Main body text                                      |
| `mentions` | list\[string\] | No       | Email addresses of users to `@mention`              |

---

## Lambda 1 — Teams Incoming Webhook (`@mention` via email)

### How it works

1. Receives SNS record and parses the JSON payload.
2. For each email address in `mentions`, derives the display name from the
   local part (e.g. `alice@example.com` → `alice`) and builds a Teams mention
   entity with `mentioned.id` set to the email address directly — Teams accepts
   an email address as a valid user identifier in Adaptive Card mention entities,
   so **no Microsoft Graph API call is required**.
3. Constructs an **Adaptive Card** (schema 1.0) with `msteams.entities` entries
   for each mention, so Teams renders the familiar `@alice` mention pill.
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

1. Receives the SNS record and parses the payload.
2. POSTs the `{ title, message, mentions }` object to the Power Automate
   instant-cloud-flow HTTP trigger URL stored in SSM.
3. The flow (which you configure in Power Automate) posts a message using the
   **Teams Flow Bot** connector and can use the `mentions` list to `@mention`
   specific users.

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
4. In the trigger card, set **Request Body JSON Schema**:

```json
{
  "type": "object",
  "properties": {
    "title":    { "type": "string" },
    "message":  { "type": "string" },
    "mentions": {
      "type": "array",
      "items": { "type": "string" }
    }
  }
}
```

5. **Save** the flow — the trigger URL is generated and shown in the trigger card.
   Copy this URL and store it as `var.flow_trigger_url` in Terraform.

#### Step 2 — Add a Teams "Post message in a chat or channel" action

1. Click **+ New step**.
2. Search for **Microsoft Teams** and choose **"Post message in a chat or channel"**.
3. Configure:
   - **Post as**: `Flow bot`
   - **Post in**: `Channel` (or `Chat`)
   - **Team / Channel**: select the target team and channel.
   - **Message**: use dynamic content — insert `triggerBody()?['title']` and
     `triggerBody()?['message']` to compose the message body.

#### Step 3 — @mentioning users (Flow Bot)

The Teams Flow Bot does **not** support `@mention` entities in the same way as
incoming webhooks. To notify users you have two options:

**Option A — Adaptive Card with mention (recommended)**

1. In the Teams action, switch **Message** to an **Adaptive Card** payload.
2. Use the `mentions` array from the trigger body in a `for each` loop:
   - Call the **Microsoft Graph HTTP** connector (or the Azure AD connector)
     to look up each email: `GET https://graph.microsoft.com/v1.0/users/{email}`.
   - Build a `msteams.entities` mention entity array as a flow variable.
3. Compose the final Adaptive Card JSON referencing the mention entities, then
   pass it as the card body in the Teams action.

   > This requires the flow's connection to have `User.Read.All` delegated
   > permission in the Azure AD app used by the **HTTP with Azure AD** connector.

**Option B — Direct chat message per user**

1. Add a **"Apply to each"** loop over `triggerBody()?['mentions']`.
2. Inside the loop, add a **"Post message in a chat or channel"** action:
   - **Post as**: `Flow bot`
   - **Post in**: `Chat with Flow bot user`
   - **Recipient**: the current email address from the loop.
   - **Message**: compose a personalised message.

This sends each mentioned user a **1-on-1 chat** from the Flow Bot — no Graph
permission is required because the flow bot posts directly to that user's chat.

#### Step 4 — Secure the HTTP trigger (optional but recommended)

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

## Running Tests

```bash
pip install pytest
python -m pytest tests/ -v
```
