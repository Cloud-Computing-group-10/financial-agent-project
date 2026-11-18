"""Load the Parquet tables written by the Spark job (spark/facts_job.py) into Postgres.

  python -m finqa.load_bulk data/bulk/parquet

Run the pilot ingest first (python -m finqa.ingest): companies that are already in the database
are skipped, so the pilot companies keep their indexed text.
"""
from __future__ import annotations

import argparse
import time
from typing import Iterator

import pyarrow.dataset as ds
from dotenv import load_dotenv

from .db import STAGING, connect, init_db, load_facts


def parquet_rows(path: str) -> Iterator[dict]:
    """Rows of one Parquet dataset, read in batches so a large table never sits in memory at once."""
    for batch in ds.dataset(path, format="parquet").to_batches():
        yield from batch.to_pylist()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("parquet_dir", help="output folder of the Spark job")
    folder = parser.parse_args().parquet_dir.rstrip("/")

    start = time.time()
    with connect() as conn:
        init_db(conn)  # creates the tables if this is an empty database
        new = load_facts(conn, {table: parquet_rows(f"{folder}/{table}") for table in STAGING})
        counts = conn.execute("""SELECT (SELECT count(*) FROM companies) AS companies,
                                        (SELECT count(*) FROM documents) AS documents,
                                        (SELECT count(*) FROM figures) AS figures""").fetchone()
    print(f"new companies: {new}  in database now: {counts}  ({time.time() - start:.1f} s)")


if __name__ == "__main__":
    main()
