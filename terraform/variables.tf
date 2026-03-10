variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment (dev, staging, prod)"
  type        = string
  default     = "prod"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment must be dev, staging, or prod."
  }
}

variable "cluster_name" {
  description = "EKS cluster name"
  type        = string
  default     = "autonomous-observability"
}

variable "cluster_version" {
  description = "Kubernetes version"
  type        = string
  default     = "1.29"
}

# ── VPC ───────────────────────────────────────────────────────────────────────

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "availability_zones" {
  description = "Availability zones to deploy into (min 3 for production)"
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b", "us-east-1c"]
}

variable "public_subnet_cidrs" {
  description = "CIDR blocks for public subnets (one per AZ)"
  type        = list(string)
  default     = ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]
}

variable "private_subnet_cidrs" {
  description = "CIDR blocks for private subnets — workloads run here"
  type        = list(string)
  default     = ["10.0.11.0/24", "10.0.12.0/24", "10.0.13.0/24"]
}

variable "intra_subnet_cidrs" {
  description = "CIDR blocks for intra subnets — control plane ENIs, no internet"
  type        = list(string)
  default     = ["10.0.21.0/24", "10.0.22.0/24", "10.0.23.0/24"]
}

# ── EKS Node Groups ───────────────────────────────────────────────────────────

variable "system_node_instance_types" {
  description = "Instance types for the system node group (kube-system workloads)"
  type        = list(string)
  default     = ["m5.large"]
}

variable "system_node_min_size" {
  description = "Minimum nodes in system node group"
  type        = number
  default     = 2
}

variable "system_node_max_size" {
  description = "Maximum nodes in system node group"
  type        = number
  default     = 4
}

variable "app_node_instance_types" {
  description = "Instance types for the application node group"
  type        = list(string)
  default     = ["m5.xlarge"]
}

variable "app_node_min_size" {
  description = "Minimum nodes in application node group"
  type        = number
  default     = 2
}

variable "app_node_max_size" {
  description = "Maximum nodes in application node group"
  type        = number
  default     = 10
}

variable "observability_node_instance_types" {
  description = "Instance types for observability stack (Prometheus is memory hungry)"
  type        = list(string)
  default     = ["r5.xlarge"]
}

variable "observability_node_min_size" {
  description = "Minimum nodes for observability"
  type        = number
  default     = 2
}

variable "observability_node_max_size" {
  description = "Maximum nodes for observability"
  type        = number
  default     = 6
}

# ── Access ────────────────────────────────────────────────────────────────────

variable "cluster_admin_arns" {
  description = "IAM ARNs that get cluster-admin access (your IAM user/role)"
  type        = list(string)
  default     = []
}
