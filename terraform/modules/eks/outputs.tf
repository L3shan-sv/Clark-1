output "cluster_name" { value = aws_eks_cluster.main.name }
output "cluster_endpoint" { value = aws_eks_cluster.main.endpoint }
output "cluster_ca_certificate" { value = aws_eks_cluster.main.certificate_authority[0].data }
output "cluster_token" {
  value     = data.aws_caller_identity.current.account_id
  sensitive = true
}
output "oidc_provider_arn" { value = aws_iam_openid_connect_provider.eks.arn }
output "oidc_provider_url" { value = aws_iam_openid_connect_provider.eks.url }
output "node_role_arn" { value = aws_iam_role.node.arn }
