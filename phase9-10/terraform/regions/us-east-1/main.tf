# terraform/regions/us-east-1/main.tf
# Regional stack — identical structure deployed to us-east-1, us-west-2, eu-west-1
# Each region is a complete, independently functional deployment.
# The only cross-region coupling is DynamoDB Global Tables for shared state.

terraform {
  required_version = ">= 1.6"
  backend "s3" {
    # Each region has its own state file
    bucket         = "autonomous-observability-tfstate-global"
    key            = "regions/us-east-1/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "terraform-state-lock"
    encrypt        = true
  }
}

locals {
  region      = "us-east-1"
  cluster_name = "autonomous-observability-${local.region}"
  cells       = ["cell-0", "cell-1", "cell-2"]   # 3 cells per region

  tags = {
    Region      = local.region
    Project     = "autonomous-observability-platform"
    ManagedBy   = "terraform"
  }
}

provider "aws" {
  region = local.region
}

# ── VPC — identical to Phase 0 but with cross-region peering ──────────────────
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = "autonomous-observability-${local.region}"
  cidr = "10.10.0.0/16"    # us-east-1 gets 10.10.x.x
  # us-west-2 uses 10.20.0.0/16, eu-west-1 uses 10.30.0.0/16
  # Non-overlapping CIDRs needed for VPC peering

  azs             = ["${local.region}a", "${local.region}b", "${local.region}c"]
  private_subnets = ["10.10.1.0/24", "10.10.2.0/24", "10.10.3.0/24"]
  public_subnets  = ["10.10.11.0/24", "10.10.12.0/24", "10.10.13.0/24"]
  intra_subnets   = ["10.10.21.0/24", "10.10.22.0/24", "10.10.23.0/24"]

  enable_nat_gateway   = true
  single_nat_gateway   = false   # One NAT per AZ — no single point of failure
  enable_dns_hostnames = true
  enable_dns_support   = true

  # Required for EKS node-to-node communication
  private_subnet_tags = {
    "kubernetes.io/cluster/${local.cluster_name}" = "owned"
    "kubernetes.io/role/internal-elb"             = "1"
  }

  public_subnet_tags = {
    "kubernetes.io/cluster/${local.cluster_name}" = "owned"
    "kubernetes.io/role/elb"                      = "1"
  }

  tags = local.tags
}

# ── EKS Cluster ───────────────────────────────────────────────────────────────
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = local.cluster_name
  cluster_version = "1.29"

  vpc_id                         = module.vpc.vpc_id
  subnet_ids                     = module.vpc.private_subnets
  cluster_endpoint_public_access = true

  # Secret encryption with regional KMS key
  cluster_encryption_config = {
    resources        = ["secrets"]
    provider_key_arn = aws_kms_key.eks.arn
  }

  eks_managed_node_groups = {
    system = {
      name           = "system"
      instance_types = ["m5.large"]
      min_size       = 2
      max_size       = 4
      desired_size   = 2
      labels         = { role = "system" }
      taints         = [{ key = "system", value = "true", effect = "NO_SCHEDULE" }]
    }

    app = {
      name           = "app"
      instance_types = ["m5.xlarge"]
      min_size       = 3
      max_size       = 30    # Higher max for multi-region failover capacity
      desired_size   = 6     # 2 nodes per cell × 3 cells
      labels         = { role = "app" }
    }

    observability = {
      name           = "observability"
      instance_types = ["r5.xlarge"]
      min_size       = 2
      max_size       = 6
      desired_size   = 2
      labels         = { role = "observability" }
      taints         = [{ key = "observability", value = "true", effect = "NO_SCHEDULE" }]
    }
  }

  tags = local.tags
}

resource "aws_kms_key" "eks" {
  description             = "EKS cluster encryption — ${local.region}"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = local.tags
}

# ── Cell namespaces — one per cell within the region ─────────────────────────
# Each cell is an isolated namespace with its own deployments
# Cells share the same node groups (resource efficiency)
# but are logically isolated (separate deployments, services, quotas)

resource "kubernetes_namespace" "cells" {
  for_each = toset(local.cells)

  metadata {
    name = each.value
    labels = {
      cell             = each.value
      region           = local.region
      "istio-injection" = "enabled"
    }
  }
}

# ── ResourceQuota per cell — prevent one cell consuming all resources ──────────
resource "kubernetes_resource_quota" "cell_quota" {
  for_each = toset(local.cells)

  metadata {
    name      = "cell-quota"
    namespace = each.value
  }

  spec {
    hard = {
      "requests.cpu"    = "8"      # 8 cores max per cell
      "requests.memory" = "16Gi"   # 16 GiB max per cell
      "pods"            = "50"     # 50 pods max per cell
    }
  }
}

# ── MSK (Managed Kafka) — one cluster per region ──────────────────────────────
# Each region has its own Kafka cluster
# Producers write to local Kafka; consumers read from local
# Cross-region replication via MirrorMaker 2 for audit events only

resource "aws_msk_cluster" "main" {
  cluster_name           = "autonomous-observability-${local.region}"
  kafka_version          = "3.5.1"
  number_of_broker_nodes = 3   # One per AZ

  broker_node_group_info {
    instance_type  = "kafka.m5.xlarge"
    client_subnets = module.vpc.private_subnets
    storage_info {
      ebs_storage_info {
        volume_size = 500   # 500 GiB per broker
        provisioned_throughput {
          enabled           = true
          volume_throughput = 250
        }
      }
    }
    security_groups = [aws_security_group.msk.id]
  }

  encryption_info {
    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
    encryption_at_rest_kms_key_arn = aws_kms_key.eks.arn
  }

  enhanced_monitoring = "PER_TOPIC_PER_BROKER"

  broker_logs {
    cloudwatch_logs {
      enabled   = true
      log_group = "/msk/autonomous-observability-${local.region}"
    }
  }

  tags = local.tags
}

resource "aws_security_group" "msk" {
  name        = "msk-${local.region}"
  vpc_id      = module.vpc.vpc_id
  description = "MSK Kafka security group"

  ingress {
    from_port       = 9094   # TLS Kafka port
    to_port         = 9094
    protocol        = "tcp"
    security_groups = [module.eks.node_security_group_id]
  }

  tags = local.tags
}

# ── ElastiCache Redis (cluster mode) — one per region ─────────────────────────
resource "aws_elasticache_replication_group" "main" {
  replication_group_id = "autonomous-observability-${local.region}"
  description          = "Redis cache — ${local.region}"

  node_type            = "cache.r6g.xlarge"
  num_cache_clusters   = 3    # Primary + 2 replicas across AZs
  automatic_failover_enabled = true
  multi_az_enabled     = true

  at_rest_encryption_enabled  = true
  transit_encryption_enabled  = true
  auth_token                  = data.aws_secretsmanager_secret_version.redis_auth.secret_string

  parameter_group_name = aws_elasticache_parameter_group.redis7.name
  subnet_group_name    = aws_elasticache_subnet_group.main.name

  log_delivery_configuration {
    destination      = "/elasticache/autonomous-observability-${local.region}"
    destination_type = "cloudwatch-logs"
    log_format       = "json"
    log_type         = "slow-log"
  }

  tags = local.tags
}

resource "aws_elasticache_parameter_group" "redis7" {
  name   = "autonomous-observability-redis7-${local.region}"
  family = "redis7"

  parameter {
    name  = "maxmemory-policy"
    value = "allkeys-lru"
  }
  parameter {
    name  = "activedefrag"
    value = "yes"
  }
}

resource "aws_elasticache_subnet_group" "main" {
  name       = "autonomous-observability-${local.region}"
  subnet_ids = module.vpc.private_subnets
}

data "aws_secretsmanager_secret_version" "redis_auth" {
  secret_id = "autonomous-observability/redis-auth-token"
}

# ── RDS Aurora Global (write to primary, read from regional replicas) ──────────
# Primary writer in us-east-1, read replicas in us-west-2 and eu-west-1
# Failover promotes a replica to writer in < 1 minute

resource "aws_rds_global_cluster" "main" {
  # Only created in us-east-1 (primary region)
  count                     = local.region == "us-east-1" ? 1 : 0
  global_cluster_identifier = "autonomous-observability-global"
  engine                    = "aurora-postgresql"
  engine_version            = "15.4"
  database_name             = "appdb"
  storage_encrypted         = true
}

resource "aws_rds_cluster" "main" {
  cluster_identifier      = "autonomous-observability-${local.region}"
  engine                  = "aurora-postgresql"
  engine_version          = "15.4"
  global_cluster_identifier = local.region == "us-east-1" ? aws_rds_global_cluster.main[0].id : null

  database_name   = local.region == "us-east-1" ? "appdb" : null
  master_username = local.region == "us-east-1" ? "app_admin" : null
  master_password = local.region == "us-east-1" ? data.aws_secretsmanager_secret_version.db_password.secret_string : null

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]

  backup_retention_period      = 7
  preferred_backup_window      = "02:00-03:00"
  preferred_maintenance_window = "sun:04:00-sun:05:00"
  skip_final_snapshot          = false
  final_snapshot_identifier    = "autonomous-observability-${local.region}-final"
  storage_encrypted            = true
  kms_key_id                   = aws_kms_key.eks.arn
  deletion_protection          = true

  enabled_cloudwatch_logs_exports = ["postgresql"]

  tags = local.tags
}

resource "aws_rds_cluster_instance" "main" {
  count              = 2   # 1 writer + 1 reader per region
  identifier         = "autonomous-observability-${local.region}-${count.index}"
  cluster_identifier = aws_rds_cluster.main.id
  instance_class     = "db.r6g.xlarge"
  engine             = aws_rds_cluster.main.engine

  performance_insights_enabled = true
  monitoring_interval          = 60
  monitoring_role_arn          = aws_iam_role.rds_monitoring.arn

  tags = local.tags
}

resource "aws_db_subnet_group" "main" {
  name       = "autonomous-observability-${local.region}"
  subnet_ids = module.vpc.intra_subnets    # Database tier — most restricted
  tags       = local.tags
}

resource "aws_security_group" "rds" {
  name   = "rds-${local.region}"
  vpc_id = module.vpc.vpc_id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [module.eks.node_security_group_id]
  }

  tags = local.tags
}

data "aws_secretsmanager_secret_version" "db_password" {
  secret_id = "autonomous-observability/db-master-password"
}

resource "aws_iam_role" "rds_monitoring" {
  name = "rds-monitoring-${local.region}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "monitoring.rds.amazonaws.com" }
    }]
  })
  managed_policy_arns = ["arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"]
}

# ── Outputs ───────────────────────────────────────────────────────────────────
output "cluster_name"       { value = module.eks.cluster_name }
output "cluster_endpoint"   { value = module.eks.cluster_endpoint }
output "kafka_bootstrap"    { value = aws_msk_cluster.main.bootstrap_brokers_tls }
output "redis_endpoint"     { value = aws_elasticache_replication_group.main.primary_endpoint_address }
output "rds_writer_endpoint" { value = aws_rds_cluster.main.endpoint }
output "rds_reader_endpoint" { value = aws_rds_cluster.main.reader_endpoint }
output "vpc_id"             { value = module.vpc.vpc_id }
