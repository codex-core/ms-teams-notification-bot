# ──────────────────────────────────────────────
# SSM Parameter Store – secrets & config
# ──────────────────────────────────────────────

resource "aws_ssm_parameter" "teams_webhook_url" {
  name        = "/teams-bot/webhook-url"
  description = "Microsoft Teams Incoming Webhook URL (or Power Automate HTTP trigger URL)"
  type        = "SecureString"
  value       = var.teams_webhook_url

  tags = local.common_tags
}

resource "aws_ssm_parameter" "flow_trigger_url" {
  name        = "/teams-bot/flow-trigger-url"
  description = "Power Automate flow HTTP trigger URL"
  type        = "SecureString"
  value       = var.flow_trigger_url

  tags = local.common_tags
}

# ──────────────────────────────────────────────
# SNS Topic
# ──────────────────────────────────────────────

resource "aws_sns_topic" "teams_notifications" {
  name = "teams-notifications-${var.environment}"
  tags = local.common_tags
}

# ──────────────────────────────────────────────
# IAM – shared Lambda execution role
# ──────────────────────────────────────────────

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_exec" {
  name               = "teams-bot-lambda-exec-${var.environment}"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "lambda_basic_execution" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "lambda_ssm" {
  statement {
    actions = ["ssm:GetParameter"]
    resources = [
      aws_ssm_parameter.teams_webhook_url.arn,
      aws_ssm_parameter.flow_trigger_url.arn,
    ]
  }
  statement {
    actions = ["kms:Decrypt"]
    # Allow decryption of SecureString params using the default SSM KMS key
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.aws_region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_policy" "lambda_ssm" {
  name        = "teams-bot-lambda-ssm-${var.environment}"
  description = "Allow Lambda to read SSM SecureString parameters"
  policy      = data.aws_iam_policy_document.lambda_ssm.json
}

resource "aws_iam_role_policy_attachment" "lambda_ssm" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = aws_iam_policy.lambda_ssm.arn
}

# ──────────────────────────────────────────────
# Lambda – Teams Webhook (with @mention support)
# ──────────────────────────────────────────────

data "archive_file" "teams_webhook_lambda" {
  type        = "zip"
  source_file = "${path.root}/../lambdas/teams_webhook_lambda/handler.py"
  output_path = "${path.root}/../dist/teams_webhook_lambda.zip"
}

resource "aws_lambda_function" "teams_webhook" {
  function_name    = "teams-webhook-notification-${var.environment}"
  description      = "Sends Adaptive Card to Teams channel via Incoming Webhook; supports @mention by email"
  filename         = data.archive_file.teams_webhook_lambda.output_path
  source_code_hash = data.archive_file.teams_webhook_lambda.output_base64sha256
  handler          = "handler.handler"
  runtime          = "python3.12"
  role             = aws_iam_role.lambda_exec.arn
  timeout          = var.lambda_timeout
  memory_size      = var.lambda_memory_size

  environment {
    variables = {
      TEAMS_WEBHOOK_URL_PARAM = aws_ssm_parameter.teams_webhook_url.name
    }
  }

  tags = local.common_tags
}

resource "aws_lambda_permission" "sns_teams_webhook" {
  statement_id  = "AllowSNSInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.teams_webhook.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.teams_notifications.arn
}

resource "aws_sns_topic_subscription" "teams_webhook" {
  topic_arn = aws_sns_topic.teams_notifications.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.teams_webhook.arn
}

# ──────────────────────────────────────────────
# Lambda – Power Automate Flow Bot
# ──────────────────────────────────────────────

data "archive_file" "flow_bot_lambda" {
  type        = "zip"
  source_file = "${path.root}/../lambdas/flow_bot_lambda/handler.py"
  output_path = "${path.root}/../dist/flow_bot_lambda.zip"
}

resource "aws_lambda_function" "flow_bot" {
  function_name    = "teams-flow-bot-notification-${var.environment}"
  description      = "Triggers a Power Automate flow to send Teams Flow Bot messages"
  filename         = data.archive_file.flow_bot_lambda.output_path
  source_code_hash = data.archive_file.flow_bot_lambda.output_base64sha256
  handler          = "handler.handler"
  runtime          = "python3.12"
  role             = aws_iam_role.lambda_exec.arn
  timeout          = var.lambda_timeout
  memory_size      = var.lambda_memory_size

  environment {
    variables = {
      FLOW_TRIGGER_URL_PARAM = aws_ssm_parameter.flow_trigger_url.name
    }
  }

  tags = local.common_tags
}

resource "aws_lambda_permission" "sns_flow_bot" {
  statement_id  = "AllowSNSInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.flow_bot.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.teams_notifications.arn
}

resource "aws_sns_topic_subscription" "flow_bot" {
  topic_arn = aws_sns_topic.teams_notifications.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.flow_bot.arn
}

# ──────────────────────────────────────────────
# Locals
# ──────────────────────────────────────────────

locals {
  common_tags = {
    Project     = "ms-teams-notification-bot"
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}
