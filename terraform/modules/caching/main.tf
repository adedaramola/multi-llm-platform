# ── ElastiCache Serverless (Redis — exact match cache) ────────────────────────
resource "aws_elasticache_serverless_cache" "redis" {
  engine = "redis"
  name   = "ai-platform-${var.environment}"

  cache_usage_limits {
    data_storage {
      maximum = 5 # GB — scale up as cache grows
      unit    = "GB"
    }
    ecpu_per_second {
      maximum = 5000 # ECPUs — auto-scales within this limit
    }
  }

  subnet_ids         = var.private_subnet_ids
  security_group_ids = [var.cache_sg_id]

  tags = { Name = "ai-platform-cache-${var.environment}" }
}

# ── Aurora Serverless v2 PostgreSQL (pgvector semantic cache) ─────────────────
resource "aws_rds_cluster" "pgvector" {
  cluster_identifier          = "ai-platform-pgvector-${var.environment}"
  engine                      = "aurora-postgresql"
  engine_version              = "16.9"
  engine_mode                 = "provisioned" # required for Serverless v2
  database_name               = "ai_platform"
  master_username             = "platform_admin"
  manage_master_user_password = true # Secrets Manager rotation built-in
  vpc_security_group_ids      = [var.cache_sg_id]
  db_subnet_group_name        = aws_db_subnet_group.pgvector.name
  storage_encrypted           = true
  deletion_protection         = var.environment != "production"
  skip_final_snapshot         = var.environment != "production"
  final_snapshot_identifier   = var.environment == "production" ? "ai-platform-final-${formatdate("YYYYMMDD", timestamp())}" : null

  enable_http_endpoint = true # RDS Data API — allows migration without bastion host

  serverlessv2_scaling_configuration {
    min_capacity = 0.5 # ~$0.07/hr minimum when active
    max_capacity = 4   # scale up to 4 ACUs under load
  }
}

resource "aws_rds_cluster_instance" "pgvector" {
  cluster_identifier = aws_rds_cluster.pgvector.id
  instance_class     = "db.serverless"
  engine             = aws_rds_cluster.pgvector.engine
  engine_version     = aws_rds_cluster.pgvector.engine_version
}

resource "aws_db_subnet_group" "pgvector" {
  name       = "ai-platform-pgvector-${var.environment}"
  subnet_ids = var.private_subnet_ids
}

# ── Secret for pg DSN ─────────────────────────────────────────────────────────
# Aurora manages master user secret automatically when manage_master_user_password=true
# We output the secret ARN so Lambda can retrieve the DSN at runtime.
