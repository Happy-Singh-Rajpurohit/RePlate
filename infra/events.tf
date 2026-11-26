resource "aws_sns_topic" "events" { name = "${var.name}-events" }
resource "aws_sqs_queue" "dead_letters" {
  name                      = "${var.name}-dead-letters"
  message_retention_seconds = 1209600
}
resource "aws_sqs_queue" "events" {
  name                       = "${var.name}-events"
  visibility_timeout_seconds = 180
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dead_letters.arn
    maxReceiveCount     = 5
  })
}
resource "aws_sqs_queue_policy" "events" {
  queue_url = aws_sqs_queue.events.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Principal = { Service = "sns.amazonaws.com" },
      Action    = "sqs:SendMessage", Resource = aws_sqs_queue.events.arn,
      Condition = { ArnEquals = { "aws:SourceArn" = aws_sns_topic.events.arn } }
    }]
  })
}
resource "aws_sns_topic_subscription" "events" {
  topic_arn            = aws_sns_topic.events.arn
  protocol             = "sqs"
  endpoint             = aws_sqs_queue.events.arn
  raw_message_delivery = true
  depends_on           = [aws_sqs_queue_policy.events]
}
resource "aws_iam_role_policy" "enqueue" {
  role = aws_iam_role.roles["enqueue"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow", Resource = aws_dynamodb_table.app.stream_arn,
        Action = ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:ListStreams"]
      },
      { Effect = "Allow", Action = "sns:Publish", Resource = aws_sns_topic.events.arn },
      { Effect = "Allow", Action = "sqs:SendMessage", Resource = aws_sqs_queue.dead_letters.arn }
    ]
  })
}
resource "aws_iam_role_policy" "match" {
  role = aws_iam_role.roles["match"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Effect = "Allow", Resource = local.db_arns,
        Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"]
      },
      {
        Effect = "Allow", Resource = aws_sqs_queue.events.arn,
        Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
      }
    ], var.enable_sms ? [{ Effect = "Allow", Action = "sns:Publish", Resource = "*" }] : [])
  })
}
data "archive_file" "worker" {
  type        = "zip"
  output_path = "${path.module}/worker.zip"
  source {
    content  = file("${path.module}/../app/worker.py")
    filename = "app/worker.py"
  }
  source {
    content  = file("${path.module}/../app/domain.py")
    filename = "app/domain.py"
  }
  source {
    content  = file("${path.module}/../app/storage.py")
    filename = "app/storage.py"
  }
}
resource "aws_cloudwatch_log_group" "worker" {
  for_each          = toset(["enqueue", "match"])
  name              = "/aws/lambda/${var.name}-${each.key}"
  retention_in_days = 14
}
resource "aws_lambda_function" "worker" {
  for_each         = aws_cloudwatch_log_group.worker
  function_name    = "${var.name}-${each.key}"
  role             = aws_iam_role.roles[each.key].arn
  handler          = "app.worker.${each.key}"
  runtime          = "python3.12"
  filename         = data.archive_file.worker.output_path
  source_code_hash = data.archive_file.worker.output_base64sha256
  timeout          = 30
  environment {
    variables = {
      TABLE_NAME  = aws_dynamodb_table.app.name
      EVENT_TOPIC = aws_sns_topic.events.arn
      ENABLE_SMS  = tostring(var.enable_sms)
    }
  }
}
resource "aws_lambda_event_source_mapping" "stream" {
  event_source_arn               = aws_dynamodb_table.app.stream_arn
  function_name                  = aws_lambda_function.worker["enqueue"].arn
  starting_position              = "TRIM_HORIZON"
  batch_size                     = 10
  bisect_batch_on_function_error = true
  maximum_retry_attempts         = 5
  destination_config {
    on_failure { destination_arn = aws_sqs_queue.dead_letters.arn }
  }
  depends_on = [aws_iam_role_policy.enqueue, aws_sns_topic_subscription.events]
}
resource "aws_lambda_event_source_mapping" "queue" {
  event_source_arn        = aws_sqs_queue.events.arn
  function_name           = aws_lambda_function.worker["match"].arn
  batch_size              = 5
  function_response_types = ["ReportBatchItemFailures"]
  depends_on              = [aws_iam_role_policy.match]
}
