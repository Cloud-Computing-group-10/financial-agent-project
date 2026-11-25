#!/usr/bin/env bash
# Download SEC's bulk files into data/bulk (about 3 GB zipped):
#   companyfacts.zip     XBRL numbers of every filer       -> unzipped into data/bulk/companyfacts/
#   submissions.zip      every filer's list of filings      (read zipped by finqa.filings)
#   company_tickers.json ticker -> CIK
# SEC wants a contact in the User-Agent; it is read from SEC_USER_AGENT in .env.
#   bash cloud/fetch_bulk.sh
set -euo pipefail
cd "$(dirname "$0")/.."

AGENT="$(grep '^SEC_USER_AGENT=' .env | cut -d= -f2- | tr -d '"')"
[ -n "$AGENT" ] || { echo "Set SEC_USER_AGENT in .env first (your name and email)"; exit 1; }

mkdir -p data/bulk
cd data/bulk
for file in xbrl/companyfacts.zip bulkdata/submissions.zip; do
  curl -fSL -A "$AGENT" -o "$(basename "$file")" "https://www.sec.gov/Archives/edgar/daily-index/$file"
done
curl -fSL -A "$AGENT" -o company_tickers.json https://www.sec.gov/files/company_tickers.json

rm -rf companyfacts && mkdir companyfacts
unzip -q companyfacts.zip -d companyfacts && rm companyfacts.zip
echo "companyfacts: $(ls companyfacts | wc -l) files, submissions.zip: $(du -h submissions.zip | cut -f1)"
