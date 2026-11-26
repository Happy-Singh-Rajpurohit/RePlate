resource "aws_security_group" "gateway" {
  count  = var.use_api_gateway ? 1 : 0
  name   = "${var.name}-gateway"
  vpc_id = aws_vpc.app.id
}
resource "aws_vpc_security_group_egress_rule" "gateway" {
  count                        = var.use_api_gateway ? 1 : 0
  security_group_id            = aws_security_group.gateway[0].id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 80
  to_port                      = 80
}
resource "aws_vpc_security_group_ingress_rule" "gateway" {
  count                        = var.use_api_gateway ? 1 : 0
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.gateway[0].id
  ip_protocol                  = "tcp"
  from_port                    = 80
  to_port                      = 80
}
resource "aws_apigatewayv2_vpc_link" "portal" {
  count              = var.use_api_gateway ? 1 : 0
  name               = var.name
  security_group_ids = aws_security_group.gateway[*].id
  subnet_ids         = aws_subnet.origin[*].id
}
resource "aws_apigatewayv2_api" "portal" {
  count         = var.use_api_gateway ? 1 : 0
  name          = var.name
  protocol_type = "HTTP"
}
resource "aws_apigatewayv2_integration" "portal" {
  count                  = var.use_api_gateway ? 1 : 0
  api_id                 = aws_apigatewayv2_api.portal[0].id
  integration_type       = "HTTP_PROXY"
  integration_uri        = aws_lb_listener.app.arn
  integration_method     = "ANY"
  connection_type        = "VPC_LINK"
  connection_id          = aws_apigatewayv2_vpc_link.portal[0].id
  payload_format_version = "1.0"
  request_parameters     = { "overwrite:path" = "$request.path" }
}
resource "aws_apigatewayv2_route" "portal" {
  count     = var.use_api_gateway ? 1 : 0
  api_id    = aws_apigatewayv2_api.portal[0].id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.portal[0].id}"
}
resource "aws_apigatewayv2_stage" "portal" {
  count       = var.use_api_gateway ? 1 : 0
  api_id      = aws_apigatewayv2_api.portal[0].id
  name        = "$default"
  auto_deploy = true
}
