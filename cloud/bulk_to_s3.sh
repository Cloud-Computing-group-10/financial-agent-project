#!/usr/bin/env bash
# Download the SEC bulk XBRL file (every filer, ~1.4 GB zipped) and put it in S3 for the Spark job.
# Run it on the EC2 instance (fast network, and LabInstanceProfile gives it S3 access):
#   bash cloud/bulk_to_s3.sh my-bucket "Your Name your.email@connect.ust.hk"
set -euo pipefail

BUCKET="$1"
USER_AGENT="$2"   # SEC requires a contact in the User-Agent
WORK="${WORK:-$HOME/bulk}"

mkdir -p "$WORK/companyfacts"
cd "$WORK"
curl -fSL -A "$USER_AGENT" -o companyfacts.zip https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip
curl -fSL -A "$USER_AGENT" -o company_tickers.json https://www.sec.gov/files/company_tickers.json
unzip -q -o companyfacts.zip -d companyfacts

aws s3 sync companyfacts "s3://$BUCKET/companyfacts/" --only-show-errors
aws s3 cp company_tickers.json "s3://$BUCKET/company_tickers.json"
echo "Uploaded $(ls companyfacts | wc -l) company files to s3://$BUCKET/companyfacts/"

rm -rf companyfacts companyfacts.zip   # free the disk; S3 is the copy we keep
