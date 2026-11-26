terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.73"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
  }
}
provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = var.name }
  }
}
variable "region" {
  type    = string
  default = "ap-south-1"
}
variable "name" {
  type    = string
  default = "replate"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,19}$", var.name))
    error_message = "Use 2 to 20 lowercase letters, digits or hyphens, starting with a letter."
  }
}
variable "image" {
  type        = string
  description = "Full ECR image URI including immutable tag or digest."
}
variable "enable_cache" {
  type    = bool
  default = false
}
variable "enable_sms" {
  type    = bool
  default = false
}

data "aws_caller_identity" "current" {}
data "aws_availability_zones" "available" { state = "available" }
data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}
resource "aws_vpc" "app" {
  cidr_block           = "10.74.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true
}
resource "aws_internet_gateway" "app" {
  vpc_id = aws_vpc.app.id
}
resource "aws_subnet" "app" {
  count             = 2
  vpc_id            = aws_vpc.app.id
  cidr_block        = cidrsubnet(aws_vpc.app.cidr_block, 8, count.index)
  availability_zone = data.aws_availability_zones.available.names[count.index]
}
resource "aws_subnet" "origin" {
  count             = 2
  vpc_id            = aws_vpc.app.id
  cidr_block        = cidrsubnet(aws_vpc.app.cidr_block, 8, count.index + 10)
  availability_zone = data.aws_availability_zones.available.names[count.index]
}
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.app.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.app.id
  }
}
resource "aws_route_table_association" "public" {
  count          = 2
  route_table_id = aws_route_table.public.id
  subnet_id      = aws_subnet.app[count.index].id
}
resource "aws_security_group" "alb" {
  name   = "${var.name}-alb"
  vpc_id = aws_vpc.app.id
}
resource "aws_security_group" "service" {
  name   = "${var.name}-service"
  vpc_id = aws_vpc.app.id
}
resource "aws_vpc_security_group_ingress_rule" "origin" {
  security_group_id = aws_security_group.alb.id
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront.id
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}
resource "aws_vpc_security_group_egress_rule" "to_service" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.service.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}
resource "aws_vpc_security_group_ingress_rule" "service" {
  security_group_id            = aws_security_group.service.id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}
resource "aws_vpc_security_group_egress_rule" "service" {
  security_group_id = aws_security_group.service.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
resource "aws_lb" "app" {
  name                       = var.name
  load_balancer_type         = "application"
  subnets                    = aws_subnet.origin[*].id
  internal                   = true
  security_groups            = [aws_security_group.alb.id]
  drop_invalid_header_fields = true
}
resource "aws_lb_target_group" "app" {
  name        = var.name
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.app.id
  health_check { path = "/health" }
}
resource "aws_lb_listener" "app" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}
resource "aws_cloudfront_vpc_origin" "app" {
  vpc_origin_endpoint_config {
    name                   = var.name
    arn                    = aws_lb.app.arn
    http_port              = 80
    https_port             = 443
    origin_protocol_policy = "http-only"
    origin_ssl_protocols {
      items    = ["TLSv1.2"]
      quantity = 1
    }
  }
  depends_on = [aws_internet_gateway.app, aws_vpc_security_group_ingress_rule.origin, aws_lb_listener.app]
}
data "aws_cloudfront_cache_policy" "no_cache" { name = "Managed-CachingDisabled" }
data "aws_cloudfront_origin_request_policy" "viewer" { name = "Managed-AllViewerExceptHostHeader" }
variable "use_api_gateway" {
  type        = bool
  default     = false
  description = "Use an API Gateway HTTPS endpoint instead of a CloudFront distribution."
}
locals {
  portal_url = var.use_api_gateway ? aws_apigatewayv2_api.portal[0].api_endpoint : "https://${aws_cloudfront_distribution.app[0].domain_name}"
}
moved {
  from = aws_cloudfront_distribution.app
  to   = aws_cloudfront_distribution.app[0]
}
resource "aws_cloudfront_distribution" "app" {
  count       = var.use_api_gateway ? 0 : 1
  enabled     = true
  price_class = "PriceClass_200"
  origin {
    domain_name = aws_lb.app.dns_name
    origin_id   = "replate-alb"
    vpc_origin_config {
      vpc_origin_id = aws_cloudfront_vpc_origin.app.id
    }
  }
  default_cache_behavior {
    target_origin_id         = "replate-alb"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.no_cache.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.viewer.id
    compress                 = true
  }
  restrictions {
    geo_restriction { restriction_type = "none" }
  }
  viewer_certificate { cloudfront_default_certificate = true }
}
resource "aws_cognito_user_pool" "app" {
  name                     = var.name
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  admin_create_user_config { allow_admin_create_user_only = true }
}
resource "aws_cognito_user_group" "roles" {
  for_each     = toset(["donor", "recipient", "processor"])
  name         = each.key
  user_pool_id = aws_cognito_user_pool.app.id
}
resource "aws_cognito_user_pool_client" "app" {
  name                                 = var.name
  user_pool_id                         = aws_cognito_user_pool.app.id
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["${local.portal_url}/"]
  logout_urls                          = ["${local.portal_url}/"]
  prevent_user_existence_errors        = "ENABLED"
  read_attributes                      = ["email", "email_verified", "phone_number", "phone_number_verified"]
  write_attributes                     = ["email"]
}
resource "aws_cognito_user_pool_domain" "app" {
  domain       = "${var.name}-${data.aws_caller_identity.current.account_id}-${var.region}"
  user_pool_id = aws_cognito_user_pool.app.id
}
resource "aws_dynamodb_table" "app" {
  name             = var.name
  billing_mode     = "PAY_PER_REQUEST"
  hash_key         = "pk"
  range_key        = "sk"
  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"
  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  attribute {
    name = "gpk"
    type = "S"
  }
  attribute {
    name = "gsk"
    type = "N"
  }
  global_secondary_index {
    name            = "Lookup"
    projection_type = "ALL"
    key_schema {
      attribute_name = "gpk"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "gsk"
      key_type       = "RANGE"
    }
  }
  point_in_time_recovery { enabled = true }
}

locals {
  db_arns = [aws_dynamodb_table.app.arn, "${aws_dynamodb_table.app.arn}/index/Lookup"]
  roles   = { execution = "ecs-tasks", api = "ecs-tasks", enqueue = "lambda", match = "lambda" }
}
resource "aws_iam_role" "roles" {
  for_each = local.roles
  name     = "${var.name}-${each.key}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRole",
      Principal = { Service = "${each.value}.amazonaws.com" }
    }]
  })
}
resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.roles["execution"].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
resource "aws_iam_role_policy_attachment" "lambda" {
  for_each   = toset(["enqueue", "match"])
  role       = aws_iam_role.roles[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
resource "aws_iam_role_policy" "api" {
  role = aws_iam_role.roles["api"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Resource = local.db_arns,
      Action = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    }]
  })
}
resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${var.name}"
  retention_in_days = 14
}
resource "aws_ecs_cluster" "app" { name = var.name }
resource "aws_ecs_task_definition" "app" {
  family                   = var.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.roles["execution"].arn
  task_role_arn            = aws_iam_role.roles["api"].arn
  container_definitions = jsonencode([{
    name         = "api", image = var.image, essential = true
    portMappings = [{ containerPort = 8080, protocol = "tcp" }]
    environment = [for key, value in {
      TABLE_NAME     = aws_dynamodb_table.app.name
      COGNITO_ISSUER = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.app.id}"
      COGNITO_CLIENT = aws_cognito_user_pool_client.app.id
      COGNITO_DOMAIN = "https://${aws_cognito_user_pool_domain.app.domain}.auth.${var.region}.amazoncognito.com"
      REDIS_URL      = var.enable_cache ? "rediss://${aws_elasticache_serverless_cache.app[0].endpoint[0].address}:6379/0" : ""
      ENABLE_SMS     = tostring(var.enable_sms)
    } : { name = key, value = value }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.api.name, awslogs-region = var.region,
        awslogs-stream-prefix = "app"
      }
    }
  }])
}
resource "aws_ecs_service" "app" {
  name                              = var.name
  cluster                           = aws_ecs_cluster.app.id
  task_definition                   = aws_ecs_task_definition.app.arn
  launch_type                       = "FARGATE"
  desired_count                     = 1
  health_check_grace_period_seconds = 60
  wait_for_steady_state             = true
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  network_configuration {
    subnets          = aws_subnet.app[*].id
    security_groups  = [aws_security_group.service.id]
    assign_public_ip = true
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "api"
    container_port   = 8080
  }
  lifecycle { ignore_changes = [desired_count] }
  depends_on = [aws_lb_listener.app, aws_route_table_association.public, aws_iam_role_policy_attachment.execution]
}
resource "aws_appautoscaling_target" "app" {
  max_capacity       = 4
  min_capacity       = 1
  resource_id        = "service/${aws_ecs_cluster.app.name}/${aws_ecs_service.app.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}
resource "aws_appautoscaling_policy" "cpu" {
  name               = "${var.name}-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.app.resource_id
  scalable_dimension = aws_appautoscaling_target.app.scalable_dimension
  service_namespace  = aws_appautoscaling_target.app.service_namespace
  target_tracking_scaling_policy_configuration {
    target_value = 60
    predefined_metric_specification { predefined_metric_type = "ECSServiceAverageCPUUtilization" }
  }
}
resource "aws_security_group" "cache" {
  count  = var.enable_cache ? 1 : 0
  name   = "${var.name}-cache"
  vpc_id = aws_vpc.app.id
}
resource "aws_vpc_security_group_ingress_rule" "cache" {
  count                        = var.enable_cache ? 1 : 0
  security_group_id            = aws_security_group.cache[0].id
  referenced_security_group_id = aws_security_group.service.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6380
}
resource "aws_elasticache_serverless_cache" "app" {
  count                = var.enable_cache ? 1 : 0
  name                 = var.name
  engine               = "valkey"
  major_engine_version = "8"
  subnet_ids           = aws_subnet.app[*].id
  security_group_ids   = aws_security_group.cache[*].id
}
output "url" { value = local.portal_url }
output "user_pool_id" { value = aws_cognito_user_pool.app.id }
output "table_name" { value = aws_dynamodb_table.app.name }
