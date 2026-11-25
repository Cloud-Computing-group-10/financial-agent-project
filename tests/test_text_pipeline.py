"""The 10-Q text pipeline for every filer: manifest, download, and loading the passages."""
import gzip
import json
import zipfile

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from conftest import A1, A2, FACTS

from finqa import db, filings
from finqa.config import EMBED_DIM
from finqa.facts import extract_company
from finqa.load_bulk import load_text_batch
from finqa.text import read_filing


def submissions_zip(path):
    """SEC submissions data for company 1: A2 in the main file, A1 in an older extra file."""
    recent = {"accessionNumber": [A2, "0000000001-23-000009"], "form": ["10-Q", "8-K"],
              "primaryDocument": ["acme-20230630.htm", "press.htm"]}
    older = {"accessionNumber": [A1], "form": ["10-Q"], "primaryDocument": ["acme-20230331.htm"]}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("CIK0000000001.json", json.dumps({"cik": "1", "filings": {"recent": recent}}))
        z.writestr("CIK0000000001-submissions-001.json", json.dumps(older))
        z.writestr("CIK0000000099.json", json.dumps({"filings": {"recent": recent}}))  # not loaded
    return path


def test_main_documents_reads_both_file_formats():
    assert filings.main_documents({"filings": {"recent": {
        "accessionNumber": [A2, "x"], "form": ["10-Q", "8-K"], "primaryDocument": ["a.htm", "b.htm"]}}}) == {A2: "a.htm"}
    assert filings.main_documents({"accessionNumber": [A1], "form": ["10-Q"], "primaryDocument": ["c.htm"]}) == {A1: "c.htm"}


def test_manifest(conn, tmp_path):
    db.load_facts(conn, extract_company(FACTS, ticker="ACME"))
    manifest = tmp_path / "manifest.csv"
    assert filings.build_manifest(conn, submissions_zip(tmp_path / "s.zip"), manifest) == 2

    batches = filings.read_manifest(manifest)
    assert [r["accession"] for r in batches[0]] == [A1, A2]
    url = conn.execute("SELECT url FROM documents WHERE accession = %s", (A2,)).fetchone()["url"]
    assert url == "https://www.sec.gov/Archives/edgar/data/1/000000000123000002/acme-20230630.htm"


def test_download_writes_gzip_and_records_failures(tmp_path, monkeypatch):
    def fake_get(url):
        if "broken" in url:
            raise OSError("404")
        return b"<html>10-Q</html>"
    monkeypatch.setattr(filings, "_get", fake_get)
    manifest = tmp_path / "manifest.csv"
    manifest.write_text(f"batch,accession,company_id,url\n0,{A1},1,https://sec/ok.htm\n0,{A2},1,https://sec/broken.htm\n")

    filings.download(manifest, tmp_path / "raw")
    folder = tmp_path / "raw" / "batch=0000"
    assert read_filing(folder / f"{A1}.htm.gz") == "<html>10-Q</html>"
    done = json.loads((folder / "_DONE").read_text())
    assert done["filings"] == 2 and done["failed"][0].startswith(A2)


def test_load_text_batch_is_idempotent(conn, tmp_path):
    db.load_facts(conn, extract_company(FACTS, ticker="ACME"))
    batch = tmp_path / "batch=0000"
    batch.mkdir()
    vector = np.ones(EMBED_DIM, dtype=np.float32) / np.sqrt(EMBED_DIM)
    pq.write_table(pa.table({
        "accession": [A2, A2, "0000000001-99-999999"],  # the last filing is not in the database
        "section": ["Part I Item 2. MD&A"] * 3, "heading": ["Results", "Results", "x"],
        "ordinal": pa.array([0, 1, 0], pa.int32()), "text": ["Sales rose.", "Costs fell.", "?"],
        "embedding": [vector.tolist()] * 3,
    }), batch / "part-0.parquet")

    assert load_text_batch(conn, batch, "bulk/text/raw/batch=0000") == 2
    assert load_text_batch(conn, batch, "bulk/text/raw/batch=0000") == 0
    doc = conn.execute("SELECT has_text, file_path FROM documents WHERE accession = %s", (A2,)).fetchone()
    assert doc == {"has_text": True, "file_path": f"bulk/text/raw/batch=0000/{A2}.htm.gz"}


def test_read_filing_plain_and_gzip(tmp_path):
    (tmp_path / "a.htm").write_text("plain")
    (tmp_path / "b.htm.gz").write_bytes(gzip.compress(b"zipped"))
    assert read_filing(tmp_path / "a.htm") == "plain"
    assert read_filing(tmp_path / "b.htm.gz") == "zipped"
