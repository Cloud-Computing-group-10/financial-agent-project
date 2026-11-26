"""Load the output of the Spark jobs into Postgres.

  python -m finqa.load_bulk facts data/bulk/parquet        numbers of every filer (spark/facts_job.py)
  python -m finqa.load_bulk text  data/bulk/text/chunks    10-Q passages (spark/text_job.py)

Both are safe to run again: companies that are already loaded are skipped, and passages are only
added to filings that have no text yet. Load the facts first, the passages refer to their filings.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Iterator

import pyarrow.dataset as ds
from dotenv import load_dotenv

from .config import DATA_DIR, EMBED_DIM, TEXT_DIR
from .db import STAGING, connect, init_db, load_facts


def parquet_rows(path: str) -> Iterator[dict]:
    """Rows of one Parquet dataset, read in batches so a large table never sits in memory at once."""
    for batch in ds.dataset(path, format="parquet").to_batches():
        yield from batch.to_pylist()


# ---------------------------------------------------------------------------- facts
def load_facts_folder(conn, folder: str) -> None:
    init_db(conn)  # creates the tables if this is an empty database
    before = conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"]
    new = load_facts(conn, {table: parquet_rows(f"{folder}/{table}") for table in STAGING})
    counts = conn.execute("""SELECT (SELECT count(*) FROM companies) AS companies,
                                    (SELECT count(*) FROM documents) AS documents,
                                    (SELECT count(*) FROM figures) AS figures""").fetchone()
    print(f"new companies: {new}  new filings: {counts['documents'] - before}  in database now: {counts}")


# ---------------------------------------------------------------------------- text
STAGE_CHUNKS = f"""
CREATE TEMP TABLE stage_chunks (accession TEXT, section TEXT, heading TEXT, ordinal INTEGER,
                                text TEXT, embedding vector({EMBED_DIM})) ON COMMIT DROP
"""
CHUNK_COLUMNS = ["accession", "section", "heading", "ordinal", "text", "embedding"]

# Passages go to filings that have no text yet, so loading a batch twice changes nothing.
INSERT_CHUNKS = """
INSERT INTO chunks (report_id, section, heading, ordinal, text, embedding)
SELECT d.report_id, s.section, s.heading, s.ordinal, s.text, s.embedding
FROM stage_chunks s JOIN documents d USING (accession)
WHERE NOT d.has_text
"""
MARK_DOCUMENTS = """
UPDATE documents d SET has_text = TRUE, file_path = %(raw_dir)s || '/' || d.accession || '.htm.gz'
FROM (SELECT DISTINCT accession FROM stage_chunks) s
WHERE d.accession = s.accession AND NOT d.has_text
"""


def load_text_batch(conn, folder: Path, raw_dir: str) -> int:
    """Load one batch folder of the text job in one transaction. Returns the passages added."""
    conn.execute(STAGE_CHUNKS)
    with conn.cursor().copy(f"COPY stage_chunks ({', '.join(CHUNK_COLUMNS)}) FROM STDIN WITH (FORMAT BINARY)") as copy:
        copy.set_types(["text", "text", "text", "int4", "text", "vector"])
        for row in parquet_rows(str(folder)):
            copy.write_row([row[name] for name in CHUNK_COLUMNS])
    added = conn.execute(INSERT_CHUNKS).rowcount
    conn.execute(MARK_DOCUMENTS, {"raw_dir": raw_dir})
    conn.commit()
    return added


def load_text_folder(conn, folder: str, raw: Path = TEXT_DIR / "raw") -> None:
    batches = sorted(p for p in Path(folder).glob("batch=*") if (p / "_SUCCESS").exists())
    raw_root = raw.resolve().relative_to(DATA_DIR.resolve())  # file_path is stored relative to data/
    for batch in batches:
        t = time.time()
        added = load_text_batch(conn, batch, f"{raw_root}/{batch.name}")
        print(f"{batch.name}: {added} passages added ({time.time() - t:.0f} s)", flush=True)
    counts = conn.execute("""SELECT (SELECT count(*) FROM documents WHERE has_text) AS filings_with_text,
                                    (SELECT count(*) FROM chunks) AS passages""").fetchone()
    print(f"in database now: {counts}")


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("kind", choices=["facts", "text"])
    parser.add_argument("folder", help="output folder of the Spark job")
    parser.add_argument("--raw", default=str(TEXT_DIR / "raw"), help="text: folder of the downloaded documents")
    args = parser.parse_args()

    start = time.time()
    with connect() as conn:
        if args.kind == "facts":
            load_facts_folder(conn, args.folder.rstrip("/"))
        else:
            load_text_folder(conn, args.folder.rstrip("/"), Path(args.raw))
    print(f"total time {time.time() - start:.1f} s")


if __name__ == "__main__":
    main()
