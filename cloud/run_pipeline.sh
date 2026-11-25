#!/usr/bin/env bash
# Build the database for every SEC filer on this machine, after cloud/fetch_bulk.sh. Every step can
# be repeated: finished work is skipped, so after a stop just start the script again.
#   nohup bash cloud/run_pipeline.sh > data/bulk/pipeline.log 2>&1 &
#
#   1. Spark facts job      companyfacts JSON  -> Parquet          (minutes)
#   2. load facts           Parquet            -> Postgres
#   3. manifest             which 10-Q documents to download
#   4. download             SEC -> data/bulk/text/raw              (hours; SEC allows 10 requests/s)
#   5. Spark text job       split + embed finished batches         (runs alongside the download)
#   6. load text            passages -> Postgres, batch by batch
set -euo pipefail
cd "$(dirname "$0")/.."

CORES="${CORES:-$(getconf _NPROCESSORS_ONLN)}"         # Spark cores
FIRST_YEAR="${FIRST_YEAR:-2019}"    # first 10-Q year to keep
BULK=data/bulk
run() { docker compose run --rm -T api "$@"; }
log() { echo "$(date '+%F %T')  $*"; }

docker compose up -d db

if [ ! -f "$BULK/parquet/documents/_SUCCESS" ]; then
  log "1. Spark facts job on $CORES cores"
  run python spark/facts_job.py --input "$BULK/companyfacts" --output "$BULK/parquet" \
      --tickers "$BULK/company_tickers.json" --first-year "$FIRST_YEAR" --cores "$CORES"
fi
log "2. load facts"
run python -m finqa.load_bulk facts "$BULK/parquet"

if [ ! -f "$BULK/text/manifest.csv" ]; then
  log "3. manifest"
  run python -m finqa.filings manifest
fi

text_round() {
  run python spark/text_job.py --input "$BULK/text/raw" --output "$BULK/text/chunks" --cores "$CORES"
  run python -m finqa.load_bulk text "$BULK/text/chunks"
}

log "4. download (log: $BULK/download.log) + 5./6. text job and load for every finished batch"
downloading() { pgrep -f "finqa.filings download" > /dev/null; }
if ! downloading; then  # after a restart the download may still be running
  run python -m finqa.filings download >> "$BULK/download.log" 2>&1 &
  sleep 30
fi
while downloading; do
  text_round
  sleep 120
done
text_round
log "done"
