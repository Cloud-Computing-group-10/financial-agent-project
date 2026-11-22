#!/usr/bin/env bash
# Copy data/bulk (raw SEC files, Parquet, passages) to S3, the long-term copy of the data lake.
# Needs an instance role with S3 access (docs/CLOUD.md). Run it again to copy only what is new.
#   bash cloud/backup_to_s3.sh my-bucket
set -euo pipefail
cd "$(dirname "$0")/.."
aws s3 sync data/bulk "s3://$1/bulk" --only-show-errors
aws s3 ls --summarize --human-readable --recursive "s3://$1/bulk" | tail -2
