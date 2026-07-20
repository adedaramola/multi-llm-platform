#!/usr/bin/env bash
set -euo pipefail

: "${AWS_REGION_NAME:?AWS_REGION_NAME is required}"
: "${CLUSTER_ARN:?CLUSTER_ARN is required}"
: "${SECRET_ARN:?SECRET_ARN is required}"
: "${DATABASE_NAME:?DATABASE_NAME is required}"

run_sql() {
  aws rds-data execute-statement \
    --region "$AWS_REGION_NAME" \
    --resource-arn "$CLUSTER_ARN" \
    --secret-arn "$SECRET_ARN" \
    --database "$DATABASE_NAME" \
    --sql "$1" \
    >/dev/null
}

run_sql 'CREATE EXTENSION IF NOT EXISTS vector'
run_sql 'CREATE TABLE IF NOT EXISTS semantic_cache (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  prompt_hash TEXT NOT NULL UNIQUE,
  embedding VECTOR(1536) NOT NULL,
  response TEXT NOT NULL,
  model_used TEXT NOT NULL,
  input_tokens INT DEFAULT 0,
  output_tokens INT DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  expires_at TIMESTAMPTZ
)'
run_sql 'CREATE INDEX IF NOT EXISTS semantic_cache_embedding_idx
  ON semantic_cache USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)'

echo "pgvector migration complete"
