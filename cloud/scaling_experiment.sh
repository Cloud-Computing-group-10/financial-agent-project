#!/usr/bin/env bash
# Spark scaling experiment (plan E5): the same work on 1, 2, 4, 8, 16 and 32 cores of this machine.
#   nohup bash cloud/scaling_experiment.sh > data/bulk/scaling.log 2>&1 &
#   text job:  split + embed a fixed sample of 100 downloaded 10-Qs (MD&A, as in the bulk run)
#   facts job: all 20,609 companyfacts files -> Parquet
# Run it when nothing else uses the cores. Results: data/bulk/scaling.csv (job, cores, seconds, rate).
set -euo pipefail
cd "$(dirname "$0")/.."

CORES_LIST="${CORES_LIST:-1 2 4 8 16 32}"
SAMPLE=data/bulk/scale/raw/batch=0000
run() { docker compose run --rm -T api "$@"; }

run sh -c "rm -rf data/bulk/scale && mkdir -p $SAMPLE && ls data/bulk/text/raw/batch=0005/*.htm.gz | head -100 | xargs -I{} cp {} $SAMPLE/ && touch $SAMPLE/_DONE"
echo "job,cores,seconds,rate" > data/bulk/scaling.csv

for cores in $CORES_LIST; do
  out=$(run python spark/text_job.py --input data/bulk/scale/raw --output /tmp/scale \
          --manifest data/bulk/text/manifest.csv --cores "$cores" 2>&1 | grep -E "^batch=0000:")
  echo "text cores=$cores: $out"
  seconds=$(echo "$out" | sed -E 's/.* ([0-9]+) s, .*/\1/')
  rate=$(echo "$out" | sed -E 's/.* ([0-9]+) passages\/s/\1/')
  echo "text,$cores,$seconds,$rate" >> data/bulk/scaling.csv
done

for cores in $CORES_LIST; do
  out=$(run python spark/facts_job.py --input data/bulk/companyfacts --output /tmp/facts \
          --tickers data/bulk/company_tickers.json --cores "$cores" 2>&1 | grep -E "total time")
  echo "facts cores=$cores: $out"
  seconds=$(echo "$out" | sed -E 's/.*total time ([0-9.]+) s/\1/')
  echo "facts,$cores,$seconds," >> data/bulk/scaling.csv
done
echo "done"
cat data/bulk/scaling.csv
