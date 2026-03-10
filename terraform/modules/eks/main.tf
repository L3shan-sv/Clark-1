# ── EKS Cluster ───────────────────────────────────────────────────────────────

data "aws_caller_identity" "current" {}

resource "aws_eks_cluster" "main" {
  name     = var.cluster_name
  version  = var.cluster_version
  role_arn = aws_iam_role.cluster.arn

  vpc_config {
    subnet_ids              = concat(var.private_subnet_ids, var.intra_subnet_ids)
    endpoint_private_access = true
    endpoint_public_access  = true # Set to false and use VPN/bastion for full private
    public_access_cidrs     = ["0.0.0.0/0"] # Restrict to your IP in production
    security_group_ids      = [aws_security_group.cluster.id]
  }

  enabled_cluster_log_types = [
    "api", "audit", "authenticator", "controllerManager", "scheduler"
  ]

  encryption_config {
    resources = ["secrets"]
    provider {
      key_arn = aws_kms_key.eks.arn
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.cluster_policy,
    aws_cloudwatch_log_group.eks,
  ]

  tags = { Name = var.cluster_name }
}

# ── KMS Key for secret encryption ────────────────────────────────────────────

resource "aws_kms_key" "eks" {
  description             = "EKS secret encryption key for ${var.cluster_name}"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  tags                    = { Name = "${var.cluster_name}-eks-key" }
}

resource "aws_kms_alias" "eks" {
  name          = "alias/${var.cluster_name}-eks"
  target_key_id = aws_kms_key.eks.key_id
}

# ── Control Plane Logs ────────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "eks" {
  name              = "/aws/eks/${var.cluster_name}/cluster"
  retention_in_days = 30
}

# ── Cluster IAM Role ──────────────────────────────────────────────────────────

resource "aws_iam_role" "cluster" {
  name = "${var.cluster_name}-cluster-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "eks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "cluster_policy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
  role       = aws_iam_role.cluster.name
}

# ── Cluster Security Group ────────────────────────────────────────────────────

resource "aws_security_group" "cluster" {
  name        = "${var.cluster_name}-cluster-sg"
  description = "EKS cluster control plane security group"
  vpc_id      = var.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.cluster_name}-cluster-sg" }
}

# ── OIDC Provider (required for IRSA) ────────────────────────────────────────

data "tls_certificate" "eks" {
  url = aws_eks_cluster.main.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "eks" {
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.eks.certificates[0].sha1_fingerprint]
  url             = aws_eks_cluster.main.identity[0].oidc[0].issuer
}

# ── Node Groups ───────────────────────────────────────────────────────────────

# Shared node IAM role
resource "aws_iam_role" "node" {
  name = "${var.cluster_name}-node-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "node_policies" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
  ])
  policy_arn = each.value
  role       = aws_iam_role.node.name
}

# System node group — runs kube-system, ingress, cert-manager
resource "aws_eks_node_group" "system" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "system"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.private_subnet_ids
  instance_types  = var.system_node_instance_types

  scaling_config {
    desired_size = var.system_node_min_size
    min_size     = var.system_node_min_size
    max_size     = var.system_node_max_size
  }

  update_config { max_unavailable = 1 }

  labels = { role = "system" }

  taint {
    key    = "CriticalAddonsOnly"
    value  = "true"
    effect = "NO_SCHEDULE"
  }

  tags = { Name = "${var.cluster_name}-system-nodes" }
}

# App node group — order, payment, notification, analytics services
resource "aws_eks_node_group" "app" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "app"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.private_subnet_ids
  instance_types  = var.app_node_instance_types

  scaling_config {
    desired_size = var.app_node_min_size
    min_size     = var.app_node_min_size
    max_size     = var.app_node_max_size
  }

  update_config { max_unavailable = 1 }

  labels = { role = "app" }

  tags = { Name = "${var.cluster_name}-app-nodes" }
}

# Observability node group — Prometheus, Loki, Tempo, Grafana
# r5.xlarge for high memory (Prometheus is memory hungry)
resource "aws_eks_node_group" "observability" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "observability"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.private_subnet_ids
  instance_types  = var.observability_node_instance_types

  scaling_config {
    desired_size = var.observability_node_min_size
    min_size     = var.observability_node_min_size
    max_size     = var.observability_node_max_size
  }

  update_config { max_unavailable = 1 }

  labels = { role = "observability" }

  taint {
    key    = "observability"
    value  = "true"
    effect = "NO_SCHEDULE"
  }

  tags = { Name = "${var.cluster_name}-observability-nodes" }
}

# ── EKS Addons ───────────────────────────────────────────────────────────────

resource "aws_eks_addon" "vpc_cni" {
  cluster_name             = aws_eks_cluster.main.name
  addon_name               = "vpc-cni"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
}

resource "aws_eks_addon" "coredns" {
  cluster_name             = aws_eks_cluster.main.name
  addon_name               = "coredns"
  resolve_conflicts_on_create = "OVERWRITE"
  depends_on               = [aws_eks_node_group.system]
}

resource "aws_eks_addon" "kube_proxy" {
  cluster_name             = aws_eks_cluster.main.name
  addon_name               = "kube-proxy"
  resolve_conflicts_on_create = "OVERWRITE"
}

resource "aws_eks_addon" "ebs_csi" {
  cluster_name             = aws_eks_cluster.main.name
  addon_name               = "aws-ebs-csi-driver"
  service_account_role_arn = aws_iam_role.ebs_csi.arn
  resolve_conflicts_on_create = "OVERWRITE"
}

# ── IRSA: EBS CSI Driver ──────────────────────────────────────────────────────

resource "aws_iam_role" "ebs_csi" {
  name = "${var.cluster_name}-ebs-csi-driver"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = aws_iam_openid_connect_provider.eks.arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:sub" = "system:serviceaccount:kube-system:ebs-csi-controller-sa"
          "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
  role       = aws_iam_role.ebs_csi.name
}
