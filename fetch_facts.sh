#!/usr/bin/env bash
set -euo pipefail

EMAIL="${1:-your.name@connect.ust.hk}"   # your real address here

curl -fsS \
  -H "User-Agent: COMP4651 Project ${EMAIL}" \
  "https://data.sec.gov/api/xbrl/companyfacts/CIK0001045810.json" \
  -o facts.json

echo "Saved facts.json"