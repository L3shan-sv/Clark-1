# ── Autonomous Observability Platform — Root Module ───────────────────────────
# This wires VPC → EKS → ALB controller together.
# After apply, run: aws eks update-kubeconfig --name <cluster_name> --region <region>

module "vpc" {
  source = "./modules/vpc"

  cluster_name         = var.cluster_name
  vpc_cidr             = var.vpc_cidr
  availability_zones   = var.availability_zones
  public_subnet_cidrs  = var.public_subnet_cidrs
  private_subnet_cidrs = var.private_subnet_cidrs
  intra_subnet_cidrs   = var.intra_subnet_cidrs
}

module "eks" {
  source = "./modules/eks"

  cluster_name    = var.cluster_name
  cluster_version = var.cluster_version
  vpc_id          = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  intra_subnet_ids   = module.vpc.intra_subnet_ids

  system_node_instance_types = var.system_node_instance_types
  system_node_min_size       = var.system_node_min_size
  system_node_max_size       = var.system_node_max_size

  app_node_instance_types = var.app_node_instance_types
  app_node_min_size       = var.app_node_min_size
  app_node_max_size       = var.app_node_max_size

  observability_node_instance_types = var.observability_node_instance_types
  observability_node_min_size       = var.observability_node_min_size
  observability_node_max_size       = var.observability_node_max_size
}

module "alb" {
  source = "./modules/alb"

  cluster_name     = var.cluster_name
  oidc_provider_arn = module.eks.oidc_provider_arn
  oidc_provider_url = module.eks.oidc_provider_url
  vpc_id           = module.vpc.vpc_id

  depends_on = [module.eks]
}

# ── Namespaces ────────────────────────────────────────────────────────────────
# Create all namespaces upfront so helm releases can target them

resource "kubernetes_namespace" "namespaces" {
  for_each = toset([
    "app",
    "observability",
    "alerting",
    "argo",
    "chaos",
    "security",
    "cost",
    "ml",
  ])

  metadata {
    name = each.value
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }

  depends_on = [module.eks]
}

# ── Storage Classes ───────────────────────────────────────────────────────────

resource "kubernetes_storage_class" "gp3" {
  metadata {
    name = "gp3"
    annotations = {
      "storageclass.kubernetes.io/is-default-class" = "true"
    }
  }

  storage_provisioner    = "ebs.csi.aws.com"
  reclaim_policy         = "Retain"
  volume_binding_mode    = "WaitForFirstConsumer"
  allow_volume_expansion = true

  parameters = {
    type      = "gp3"
    encrypted = "true"
    iops      = "3000"
    throughput = "125"
  }

  depends_on = [module.eks]
}

# ── Cluster Access ────────────────────────────────────────────────────────────

resource "kubernetes_cluster_role_binding" "admins" {
  count = length(var.cluster_admin_arns) > 0 ? 1 : 0

  metadata { name = "platform-admins" }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = "cluster-admin"
  }

  dynamic "subject" {
    for_each = var.cluster_admin_arns
    content {
      kind      = "User"
      name      = subject.value
      api_group = "rbac.authorization.k8s.io"
    }
  }

  depends_on = [module.eks]
}
