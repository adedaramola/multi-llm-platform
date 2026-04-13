# ── GitHub OIDC Provider ──────────────────────────────────────────────────────
# Allows GitHub Actions to assume an AWS IAM role without long-lived secrets.
resource "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"

  client_id_list = ["sts.amazonaws.com"]

  # AWS validates the GitHub OIDC cert automatically; thumbprint still required
  # by the resource schema. This is the well-known GitHub OIDC CA thumbprint.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

# ── IAM Role for GitHub Actions ───────────────────────────────────────────────
resource "aws_iam_role" "github_actions" {
  name = "ai-platform-github-actions"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = aws_iam_openid_connect_provider.github.arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          # Scope to main branch only — PRs cannot deploy
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:ref:refs/heads/main"
        }
      }
    }]
  })
}

# ── IAM Policy — minimal deploy permissions ───────────────────────────────────
resource "aws_iam_role_policy" "github_actions_deploy" {
  name = "ai-platform-deploy"
  role = aws_iam_role.github_actions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # All Lambda deploy actions are evaluated against the base function ARN.
        # UpdateAlias targets a named alias via request parameter, not resource ARN.
        Sid    = "LambdaDeploy"
        Effect = "Allow"
        Action = [
          "lambda:UpdateFunctionCode",
          "lambda:GetFunction",
          "lambda:GetFunctionConfiguration",
          "lambda:PublishVersion",
          "lambda:UpdateAlias",
          "lambda:GetAlias",
        ]
        Resource = [
          var.gateway_function_arn,
          var.health_checker_function_arn,
        ]
      },
    ]
  })
}
