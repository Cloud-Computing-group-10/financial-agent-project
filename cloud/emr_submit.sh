#!/usr/bin/env bash
# Upload the Spark job to S3 and run it as a step on a running EMR cluster.
#   bash cloud/emr_submit.sh my-bucket j-XXXXXXXXXXXX [first-year]
# Output: s3://my-bucket/parquet/ (tables + reports). Watch the step in the EMR console.
set -euo pipefail

BUCKET="$1"
CLUSTER_ID="$2"
FIRST_YEAR="${3:-2019}"

cd "$(dirname "$0")/.."
rm -f /tmp/finqa.zip
zip -q -r /tmp/finqa.zip finqa -x "*__pycache__*"
aws s3 cp /tmp/finqa.zip "s3://$BUCKET/code/finqa.zip"
aws s3 cp spark/facts_job.py "s3://$BUCKET/code/facts_job.py"

aws emr add-steps --cluster-id "$CLUSTER_ID" --steps "Type=Spark,Name=finqa-facts,ActionOnFailure=CONTINUE,Args=[\
--py-files,s3://$BUCKET/code/finqa.zip,\
s3://$BUCKET/code/facts_job.py,\
--input,s3://$BUCKET/companyfacts,\
--output,s3://$BUCKET/parquet,\
--tickers,s3://$BUCKET/company_tickers.json,\
--first-year,$FIRST_YEAR]"
