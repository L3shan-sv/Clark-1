# terraform/global/main.tf
#
# Global Infrastructure — Active-Active Multi-Region
#
# Architecture:
#   3 regions run identical stacks: us-east-1, us-west-2, eu-west-1
#   Each region is fully independent — can serve 100% of traffic if others fail
#   Global Accelerator routes traffic to lowest-latency healthy region
#   Route53 health checks fail traffic over in < 60 seconds
#   DynamoDB Global Tables replicate state across all regions (< 1s lag)
#
# Cell decomposition within each region:
#   Each region is split into N cells (default 3)
#   Each cell serves a subset of customers (hashed by customer_id)
#   A cell failure affects only that cell's customers — not the whole region
#   Cells can be independently deployed, scaled, and chaos-tested
#
# Why active-active and not active-passive?
#   Active-passive wastes 50% of capacity sitting idle
#   Failover takes minutes (cold start, DNS TTL)
#   Active-active: instant failover, all capacity used, globally low latency

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket         = "autonomous-observability-tfstate-global"
    key            = "global/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "terraform-state-lock"
    encrypt        = true
  }
}

# ── Regions to deploy ─────────────────────────────────────────────────────────
locals {
  regions = {
    primary   = "us-east-1"
    secondary = "us-west-2"
    tertiary  = "eu-west-1"
  }

  # Each region gets 3 cells
  cells_per_region = 3

  # Cell routing: customer_id % total_cells → cell assignment
  # Cell 0-2: us-east-1  (cells 0,1,2)
  # Cell 3-5: us-west-2  (cells 3,4,5)
  # Cell 6-8: eu-west-1  (cells 6,7,8)
  total_cells = local.cells_per_region * length(local.regions)

  tags = {
    Project     = "autonomous-observability-platform"
    ManagedBy   = "terraform"
    Architecture = "active-active-multi-region"
  }
}

# ── AWS providers for each region ─────────────────────────────────────────────
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
}

provider "aws" {
  alias  = "us_west_2"
  region = "us-west-2"
}

provider "aws" {
  alias  = "eu_west_1"
  region = "eu-west-1"
}

# ── DynamoDB Global Tables — replicated session/state store ───────────────────
# Used for: distributed rate limiting, session state, idempotency keys
# Each write replicates to all regions in < 1 second (eventually consistent)
# Reads are strongly consistent within a region

resource "aws_dynamodb_table" "order_idempotency" {
  provider         = aws.us_east_1
  name             = "order-idempotency-keys"
  billing_mode     = "PAY_PER_REQUEST"
  hash_key         = "idempotency_key"
  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"

  attribute {
    name = "idempotency_key"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  # Global replication to all regions
  replica {
    region_name = "us-west-2"
    kms_key_arn = aws_kms_key.global["us-west-2"].arn
  }

  replica {
    region_name = "eu-west-1"
    kms_key_arn = aws_kms_key.global["eu-west-1"].arn
  }

  point_in_time_recovery { enabled = true }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.global["us-east-1"].arn
  }

  tags = local.tags
}

resource "aws_dynamodb_table" "rate_limits" {
  provider         = aws.us_east_1
  name             = "distributed-rate-limits"
  billing_mode     = "PAY_PER_REQUEST"
  hash_key         = "customer_id"
  range_key        = "window"
  stream_enabled   = true
  stream_view_type = "NEW_IMAGE"

  attribute {
    name = "customer_id"
    type = "S"
  }

  attribute {
    name = "window"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  replica {
    region_name = "us-west-2"
  }

  replica {
    region_name = "eu-west-1"
  }

  tags = local.tags
}

# ── KMS keys per region (for encryption) ──────────────────────────────────────
resource "aws_kms_key" "global" {
  for_each = {
    "us-east-1" = "us_east_1"
    "us-west-2" = "us_west_2"
    "eu-west-1" = "eu_west_1"
  }

  provider                = aws.us_east_1
  description             = "Global KMS key — ${each.key}"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = local.tags
}

# ── AWS Global Accelerator — anycast routing ───────────────────────────────────
# Routes users to the lowest-latency healthy region automatically
# Anycast IP stays constant — DNS TTL is not a failover bottleneck
# Health checks happen every 10 seconds — < 30s failover

resource "aws_globalaccelerator_accelerator" "main" {
  name            = "autonomous-observability-platform"
  ip_address_type = "IPV4"
  enabled         = true

  attributes {
    flow_logs_enabled   = true
    flow_logs_s3_bucket = aws_s3_bucket.global_accelerator_logs.bucket
    flow_logs_s3_prefix = "global-accelerator/"
  }

  tags = local.tags
}

resource "aws_globalaccelerator_listener" "https" {
  accelerator_arn = aws_globalaccelerator_accelerator.main.id
  protocol        = "TCP"
  port_range {
    from_port = 443
    to_port   = 443
  }
}

resource "aws_globalaccelerator_endpoint_group" "us_east_1" {
  listener_arn                  = aws_globalaccelerator_listener.https.id
  endpoint_group_region         = "us-east-1"
  traffic_dial_percentage       = 40    # 40% of traffic to us-east-1
  health_check_interval_seconds = 10
  health_check_protocol         = "HTTPS"
  health_check_path             = "/health/live"
  threshold_count               = 2    # 2 failed checks → fail over

  endpoint_configuration {
    endpoint_id                    = data.aws_alb.us_east_1.arn
    weight                         = 100
    client_ip_preservation_enabled = true
  }
}

resource "aws_globalaccelerator_endpoint_group" "us_west_2" {
  listener_arn                  = aws_globalaccelerator_listener.https.id
  endpoint_group_region         = "us-west-2"
  traffic_dial_percentage       = 40    # 40% to us-west-2
  health_check_interval_seconds = 10
  health_check_protocol         = "HTTPS"
  health_check_path             = "/health/live"
  threshold_count               = 2

  endpoint_configuration {
    endpoint_id                    = data.aws_alb.us_west_2.arn
    weight                         = 100
    client_ip_preservation_enabled = true
  }
}

resource "aws_globalaccelerator_endpoint_group" "eu_west_1" {
  listener_arn                  = aws_globalaccelerator_listener.https.id
  endpoint_group_region         = "eu-west-1"
  traffic_dial_percentage       = 20    # 20% to eu-west-1 (GDPR region)
  health_check_interval_seconds = 10
  health_check_protocol         = "HTTPS"
  health_check_path             = "/health/live"
  threshold_count               = 2

  endpoint_configuration {
    endpoint_id                    = data.aws_alb.eu_west_1.arn
    weight                         = 100
    client_ip_preservation_enabled = true
  }
}

# ── Route53 health checks + failover ─────────────────────────────────────────
resource "aws_route53_health_check" "us_east_1" {
  fqdn              = "api-us-east-1.autonomous-observability-platform.internal"
  port              = 443
  type              = "HTTPS"
  resource_path     = "/health/live"
  failure_threshold = 2
  request_interval  = 10

  tags = merge(local.tags, { Name = "us-east-1-health-check" })
}

resource "aws_route53_health_check" "us_west_2" {
  fqdn              = "api-us-west-2.autonomous-observability-platform.internal"
  port              = 443
  type              = "HTTPS"
  resource_path     = "/health/live"
  failure_threshold = 2
  request_interval  = 10

  tags = merge(local.tags, { Name = "us-west-2-health-check" })
}

# ── S3 for logs (global) ──────────────────────────────────────────────────────
resource "aws_s3_bucket" "global_accelerator_logs" {
  provider = aws.us_east_1
  bucket   = "autonomous-observability-global-accelerator-logs"
  tags     = local.tags
}

resource "aws_s3_bucket_lifecycle_configuration" "log_retention" {
  provider = aws.us_east_1
  bucket   = aws_s3_bucket.global_accelerator_logs.id

  rule {
    id     = "expire-old-logs"
    status = "Enabled"
    expiration { days = 90 }
  }
}

# ── Data sources (ALBs created by regional EKS stacks) ───────────────────────
data "aws_alb" "us_east_1" {
  provider = aws.us_east_1
  tags     = { "kubernetes.io/cluster/autonomous-observability-us-east-1" = "owned" }
}

data "aws_alb" "us_west_2" {
  provider = aws.us_west_2
  tags     = { "kubernetes.io/cluster/autonomous-observability-us-west-2" = "owned" }
}

data "aws_alb" "eu_west_1" {
  provider = aws.eu_west_1
  tags     = { "kubernetes.io/cluster/autonomous-observability-eu-west-1" = "owned" }
}

# ── Outputs ───────────────────────────────────────────────────────────────────
output "global_accelerator_ips" {
  value       = aws_globalaccelerator_accelerator.main.ip_sets[*].ip_addresses
  description = "Anycast IPs — point your DNS CNAME here"
}

output "global_accelerator_dns" {
  value       = aws_globalaccelerator_accelerator.main.dns_name
  description = "Global Accelerator DNS name"
}

output "dynamodb_global_table_arns" {
  value = {
    order_idempotency = aws_dynamodb_table.order_idempotency.arn
    rate_limits       = aws_dynamodb_table.rate_limits.arn
  }
}
