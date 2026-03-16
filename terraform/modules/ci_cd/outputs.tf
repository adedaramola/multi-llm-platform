output "github_actions_role_arn" {
  value       = aws_iam_role.github_actions.arn
  description = "ARN to set as AWS_DEPLOY_ROLE_ARN in GitHub Actions secrets"
}
