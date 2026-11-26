"""How many 10-Qs reach the SEC per day: the load an update run has to keep up with.

  python eval/filings_per_day.py data/bulk/submissions.zip

Counts every 10-Q by filing date in SEC's submissions.zip (each company's recent filings) and writes
eval/results/filings_per_day.csv with one row per date, plus a summary of the busiest days.
"""
from __future__ import annotations

import csv
import json
import sys
import zipfile
from collections import Counter
from pathlib import Path

OUT = Path(__file__).resolve().parent / "results" / "filings_per_day.csv"


def main(path: str) -> None:
    per_day: Counter[str] = Counter()
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not name.endswith(".json") or "-submissions-" in name:
                continue  # older filings sit in the extra files; recent ones are enough here
            block = json.loads(z.read(name)).get("filings", {}).get("recent", {})
            for form, filed in zip(block.get("form", []), block.get("filingDate", [])):
                if form == "10-Q":
                    per_day[filed] += 1
    days = sorted(d for d in per_day if "2024-01-01" <= d <= "2026-12-31")
    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "filings"])
        w.writerows((d, per_day[d]) for d in days)
    for year in ("2024", "2025", "2026"):
        counts = sorted((per_day[d] for d in days if d.startswith(year)), reverse=True)
        if counts:
            print(f"{year}: {sum(counts)} 10-Qs on {len(counts)} days, busiest day {counts[0]}, "
                  f"10th busiest {counts[min(9, len(counts) - 1)]}, median day {counts[len(counts) // 2]}")
    print("busiest days:", sorted(((per_day[d], d) for d in days), reverse=True)[:10])


if __name__ == "__main__":
    main(sys.argv[1])
