#!/usr/bin/env bash
set -euo pipefail

: "${STATE_BUCKET:?STATE_BUCKET is required}"
AWS_REGION_NAME="${AWS_REGION_NAME:-us-east-1}"

if ! aws s3api head-bucket --bucket "$STATE_BUCKET" >/dev/null 2>&1; then
  if [[ "$AWS_REGION_NAME" == "us-east-1" ]]; then
    aws s3api create-bucket \
      --bucket "$STATE_BUCKET" \
      --region "$AWS_REGION_NAME" \
      >/dev/null
  else
    aws s3api create-bucket \
      --bucket "$STATE_BUCKET" \
      --region "$AWS_REGION_NAME" \
      --create-bucket-configuration "LocationConstraint=$AWS_REGION_NAME" \
      >/dev/null
  fi
fi

aws s3api put-bucket-encryption \
  --bucket "$STATE_BUCKET" \
  --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

aws s3api put-public-access-block \
  --bucket "$STATE_BUCKET" \
  --public-access-block-configuration \
    'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'

aws s3api put-bucket-versioning \
  --bucket "$STATE_BUCKET" \
  --versioning-configuration Status=Enabled

echo "Terraform state bucket is ready: encrypted, private, and versioned"
