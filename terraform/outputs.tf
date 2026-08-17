output "sns_topic_arn" {
  description = "ARN of the SNS topic that triggers both Lambda functions"
  value       = aws_sns_topic.teams_notifications.arn
}

output "teams_webhook_lambda_arn" {
  description = "ARN of the Teams Webhook Lambda function"
  value       = aws_lambda_function.teams_webhook.arn
}

output "flow_bot_lambda_arn" {
  description = "ARN of the Power Automate Flow Bot Lambda function"
  value       = aws_lambda_function.flow_bot.arn
}
