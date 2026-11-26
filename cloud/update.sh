#!/usr/bin/env bash
# Update run: add the 10-Qs filed since the last load to an existing database, and time every step.
#   nohup bash cloud/update.sh > data/bulk/update.log 2>&1 &
#
# The same steps and code as cloud/run_pipeline.sh, on new data only:
#   1. fetch SEC's bulk files again         (companyfacts.zip, submissions.zip)
#   2. Spark facts job                     all companies again (it takes under a minute)
#   3. load facts                          only filings filed after the last load are added
#   4. manifest of the new filings          finqa.filings manifest --since
#   5. download the new 10-Qs              8 requests/s
#   6. Spark text job                      passages + embeddings of the new 10-Qs
#   7. load text
# The step times go to eval/results/update_<date>.csv.
set -euo pipefail
cd "$(dirname "$0")/.."

CORES="${CORES:-$(getconf _NPROCESSORS_ONLN)}"
BULK=data/bulk
UPDATE="$BULK/text/update-$(date +%F)"
OUT="eval/results/update_$(date +%F).csv"
run() { docker compose run --rm -T api "$@"; }
sql() { docker compose exec -T db psql -U finqa -tAc "$1"; }
log() { echo "$(date '+%F %T')  $*"; }

docker compose up -d db
SINCE="$(sql "SELECT max(filed) + 1 FROM documents WHERE accession NOT LIKE 'financebench:%'")"
log "last load: filings up to $(sql "SELECT max(filed) FROM documents WHERE accession NOT LIKE 'financebench:%'"); new ones from $SINCE"
COUNTS="SELECT count(*) FROM documents WHERE accession NOT LIKE 'financebench:%'"
echo "step,seconds,detail" > "$OUT"
step() {  # step <name> <command...>: run it and append its time to the CSV
  local name="$1"; shift
  log "$name"
  local t0; t0=$(date +%s.%N)
  "$@"
  echo "$name,$(python3 -c "print(round($(date +%s.%N) - $t0, 1))"),cores=$CORES" >> "$OUT"
}
DOCS_BEFORE="$(sql "$COUNTS")"
CHUNKS_BEFORE="$(sql "SELECT count(*) FROM chunks")"

step "1 fetch bulk files" bash cloud/fetch_bulk.sh
step "2 Spark facts job" run python spark/facts_job.py --input "$BULK/companyfacts" --output "$BULK/parquet-update" \
     --tickers "$BULK/company_tickers.json" --first-year 2019 --cores "$CORES"
step "3 load facts" run python -m finqa.load_bulk facts "$BULK/parquet-update"
step "4 manifest" run python -m finqa.filings manifest --since "$SINCE" --manifest "$UPDATE/manifest.csv"
step "5 download" run python -m finqa.filings download --manifest "$UPDATE/manifest.csv" --raw "$UPDATE/raw"
step "6 Spark text job" run python spark/text_job.py --input "$UPDATE/raw" --output "$UPDATE/chunks" --cores "$CORES"
step "7 load text" run python -m finqa.load_bulk text "$UPDATE/chunks" --raw "$UPDATE/raw"

NEW_DOCS=$(( $(sql "$COUNTS") - DOCS_BEFORE ))
NEW_CHUNKS=$(( $(sql "SELECT count(*) FROM chunks") - CHUNKS_BEFORE ))
FILINGS="$(($(wc -l < "$UPDATE/manifest.csv") - 1))"
echo "result,0,new_filings=$NEW_DOCS manifest=$FILINGS new_passages=$NEW_CHUNKS since=$SINCE" >> "$OUT"
log "done: $NEW_DOCS new filings, $FILINGS to download, $NEW_CHUNKS new passages -> $OUT"
cat "$OUT"
