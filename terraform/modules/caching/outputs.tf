output "redis_endpoint" {
  value = aws_elasticache_serverless_cache.redis.endpoint[0].address
}

output "pg_secret_arn" {
  description = "ARN of the Aurora master user secret (managed by RDS)"
  value       = aws_rds_cluster.pgvector.master_user_secret[0].secret_arn
}

output "pg_cluster_endpoint" {
  value = aws_rds_cluster.pgvector.endpoint
}
