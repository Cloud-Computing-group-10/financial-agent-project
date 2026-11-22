"""10-Q documents of every filer: the list of filings to fetch, and a resumable download.

  python -m finqa.filings manifest   which 10-Q documents to download (writes text/manifest.csv)
  python -m finqa.filings download   download them from SEC into text/raw/batch=NNNN/ (resumable)

The manifest covers every 10-Q in the documents table that has no indexed text yet, so run the
facts load (python -m finqa.load_bulk facts ...) first. SEC's submissions.zip gives the file name
of each filing's main document; the manifest also stores that URL in documents.url, so answers
link to the 10-Q itself.

Filings are split into numbered batches. A batch is finished when its _DONE file exists, so a
stopped download continues where it left off, and the Spark text job (spark/text_job.py) can
start on finished batches while the download is still running.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import threading
import time
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from dotenv import load_dotenv

from .config import BULK_DIR, SEC_USER_AGENT, TEXT_DIR
from .db import connect

FORM = "10-Q"
BATCH_SIZE = 2000              # filings per batch
REQUESTS_PER_SECOND = 8        # SEC allows 10 per second per client
MANIFEST_COLUMNS = ["batch", "accession", "company_id", "url"]
SUBMISSIONS_FILE = re.compile(r"CIK(\d{10})(?:-submissions-\d+)?\.json$")


# ---------------------------------------------------------------------------- manifest
def main_documents(columns: dict) -> dict[str, str]:
    """{accession: main document file name} of the 10-Qs in one block of SEC submissions data.

    A company's file has the block under filings.recent; its older filings are in extra files
    (CIK...-submissions-001.json) that contain the block itself."""
    block = columns.get("filings", {}).get("recent", columns)
    return {accession: document
            for accession, form, document in zip(block.get("accessionNumber", []), block.get("form", []),
                                                 block.get("primaryDocument", []))
            if form == FORM and document}


def document_url(company_id: int, accession: str, document: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{company_id}/{accession.replace('-', '')}/{document}"


def build_manifest(conn, submissions_zip: Path, manifest: Path) -> int:
    """Write the manifest and store each 10-Q's document URL in documents.url. Returns the row count."""
    wanted: dict[int, set[str]] = defaultdict(set)  # company_id -> accessions without text
    for row in conn.execute("SELECT company_id, accession FROM documents WHERE NOT has_text"):
        wanted[row["company_id"]].add(row["accession"])

    rows, seen = [], set()
    with zipfile.ZipFile(submissions_zip) as z:
        for name in z.namelist():
            match = SUBMISSIONS_FILE.search(name)
            if not match or int(match.group(1)) not in wanted:
                continue
            company_id = int(match.group(1))
            for accession, document in main_documents(json.loads(z.read(name))).items():
                if accession in wanted[company_id] and accession not in seen:
                    seen.add(accession)  # a jointly filed 10-Q is downloaded once; its text goes to both companies
                    rows.append({"accession": accession, "company_id": company_id,
                                 "url": document_url(company_id, accession, document)})

    rows.sort(key=lambda r: (r["company_id"], r["accession"]))
    for i, row in enumerate(rows):
        row["batch"] = i // BATCH_SIZE
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", newline="") as f:
        writer = csv.DictWriter(f, MANIFEST_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    conn.execute("CREATE TEMP TABLE stage_urls (accession TEXT, url TEXT) ON COMMIT DROP")
    with conn.cursor().copy("COPY stage_urls (accession, url) FROM STDIN") as copy:
        for row in rows:
            copy.write_row([row["accession"], row["url"]])
    conn.execute("UPDATE documents d SET url = s.url FROM stage_urls s WHERE d.accession = s.accession")
    conn.commit()
    return len(rows)


def read_manifest(manifest: Path) -> dict[int, list[dict]]:
    """{batch: rows}"""
    batches: dict[int, list[dict]] = defaultdict(list)
    with manifest.open() as f:
        for row in csv.DictReader(f):
            batches[int(row["batch"])].append(row)
    return dict(sorted(batches.items()))


def batch_dir(root: Path, batch: int) -> Path:
    return root / f"batch={batch:04d}"


# ---------------------------------------------------------------------------- download
class Throttle:
    """At most `per_second` calls to wait() per second, shared by all threads."""

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self.lock = threading.Lock()
        self.next_time = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_time)
            self.next_time = start + self.interval
        time.sleep(start - now)


_throttle = Throttle(REQUESTS_PER_SECOND)
_local = threading.local()


def _get(url: str) -> bytes:
    """GET with the SEC user agent. Retries a few times when SEC asks us to slow down."""
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
        _local.session.headers.update({"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"})
    for attempt in range(4):
        _throttle.wait()
        r = _local.session.get(url, timeout=60)
        if r.status_code not in (429, 503):
            r.raise_for_status()
            return r.content
        time.sleep(10 * (attempt + 1))
    r.raise_for_status()
    return b""


def fetch(row: dict, folder: Path) -> str | None:
    """Download one document to folder/<accession>.htm.gz. Returns an error message or None."""
    dest = folder / f"{row['accession']}.htm.gz"
    if dest.exists():
        return None
    try:
        content = _get(row["url"])
    except Exception as e:  # one broken filing must not stop the whole download
        return f"{row['accession']}: {str(e)[:200]}"
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(content))
    tmp.rename(dest)  # a file that exists is always complete
    return None


def download(manifest: Path, raw_dir: Path, threads: int = 8) -> None:
    batches = read_manifest(manifest)
    start = time.time()
    for batch, rows in batches.items():
        folder = batch_dir(raw_dir, batch)
        done = folder / "_DONE"
        if done.exists():
            continue
        folder.mkdir(parents=True, exist_ok=True)
        t = time.time()
        with ThreadPoolExecutor(threads) as pool:
            failed = [e for e in pool.map(lambda r: fetch(r, folder), rows) if e]
        done.write_text(json.dumps({"filings": len(rows), "failed": failed}, indent=1))
        print(f"batch {batch}/{len(batches) - 1}: {len(rows)} filings, {len(failed)} failed, "
              f"{time.time() - t:.0f} s (total {(time.time() - start) / 3600:.2f} h)", flush=True)


if __name__ == "__main__":
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=["manifest", "download"])
    parser.add_argument("--submissions", default=str(BULK_DIR / "submissions.zip"))
    parser.add_argument("--manifest", default=str(TEXT_DIR / "manifest.csv"))
    parser.add_argument("--raw", default=str(TEXT_DIR / "raw"), help="where the downloaded documents go")
    args = parser.parse_args()

    if args.step == "manifest":
        with connect() as conn:
            n = build_manifest(conn, Path(args.submissions), Path(args.manifest))
        print(f"manifest: {n} filings in {(n + BATCH_SIZE - 1) // BATCH_SIZE} batches -> {args.manifest}")
    else:
        download(Path(args.manifest), Path(args.raw))
