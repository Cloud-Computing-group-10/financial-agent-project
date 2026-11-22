"""Spark job: split the downloaded 10-Qs into passages and embed them, one batch at a time.

On one machine (Spark uses N cores; leave out --cores to use all of them):
  python spark/text_job.py --input data/bulk/text/raw --output data/bulk/text/chunks --cores 16
On EMR (see docs/CLOUD.md), with s3:// paths.

Input: text/raw/batch=NNNN/<accession>.htm.gz from `python -m finqa.filings download`.
Each filing is split with finqa.text.filing_chunks() and every passage is embedded with
finqa.embed.embed(), the same code as the pilot ingest. Output: one Parquet folder per batch,
text/chunks/batch=NNNN/, with columns accession, section, heading, ordinal, text, embedding.

Only batches whose download is finished (_DONE) and that have no output yet (_SUCCESS) are
processed, so the job can run while the download continues, and a stopped run just starts again.
Load the result with:  python -m finqa.load_bulk text data/bulk/text/chunks
"""
from __future__ import annotations

import argparse
import gzip
import os
import sys
import time

from pyspark.sql import SparkSession
from pyspark.sql.types import (ArrayType, FloatType, IntegerType, StringType, StructField,
                               StructType)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # run from a checkout
from finqa.embed import embed, passage_text  # noqa: E402
from finqa.text import filing_chunks  # noqa: E402

SCHEMA = StructType([
    StructField("accession", StringType()), StructField("section", StringType()),
    StructField("heading", StringType()), StructField("ordinal", IntegerType()),
    StructField("text", StringType()), StructField("embedding", ArrayType(FloatType())),
])
EMBED_BATCH = 256  # passages per embedding call


def filing_passages(path: str, content: bytes):
    """(accession, section, heading, ordinal, text) for every passage of one downloaded filing."""
    accession = path.rsplit("/", 1)[-1].replace(".htm.gz", "")
    html = gzip.decompress(content).decode("utf-8", errors="ignore")
    for ch in filing_chunks(html):
        yield accession, ch["section"], ch["heading"], ch["ordinal"], ch["text"]


def embed_partition(rows):
    """Add the embedding to each passage. One thread per task: Spark already runs a task per core."""
    rows = list(rows)
    for i in range(0, len(rows), EMBED_BATCH):
        part = rows[i:i + EMBED_BATCH]
        vectors = embed([passage_text(r[2], r[4]) for r in part], threads=1)
        for row, vector in zip(part, vectors):
            yield (*row, vector.tolist())


# ---------------------------------------------------------------------------- batches
def _fs(spark: SparkSession, path: str):
    """Hadoop file system for a local or s3:// path, so the same code runs on a laptop and on EMR."""
    jpath = spark._jvm.org.apache.hadoop.fs.Path(path)
    return jpath.getFileSystem(spark._jsc.hadoopConfiguration()), jpath


def exists(spark: SparkSession, path: str) -> bool:
    fs, jpath = _fs(spark, path)
    return fs.exists(jpath)


def pending_batches(spark: SparkSession, raw: str, output: str) -> list[str]:
    """Batch folder names (batch=NNNN) that are downloaded but not processed yet."""
    fs, jpath = _fs(spark, raw)
    if not fs.exists(jpath):
        return []
    names = sorted(s.getPath().getName() for s in fs.listStatus(jpath) if s.isDirectory())
    return [n for n in names if n.startswith("batch=")
            and exists(spark, f"{raw}/{n}/_DONE") and not exists(spark, f"{output}/{n}/_SUCCESS")]


def process_batch(spark: SparkSession, raw: str, output: str, name: str, partitions: int) -> tuple[int, int]:
    files = spark.read.format("binaryFile").option("pathGlobFilter", "*.htm.gz").load(f"{raw}/{name}")
    passages = (files.select("path", "content").rdd
                .flatMap(lambda f: filing_passages(f.path, bytes(f.content)))
                .repartition(partitions)  # spread the passages evenly before the expensive step
                .mapPartitions(embed_partition))
    spark.createDataFrame(passages, SCHEMA).write.mode("overwrite").parquet(f"{output}/{name}")
    written = spark.read.parquet(f"{output}/{name}")
    return written.select("accession").distinct().count(), written.count()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, help="text/raw folder (local or s3://)")
    parser.add_argument("--output", required=True, help="text/chunks folder (local or s3://)")
    parser.add_argument("--cores", type=int, help="run locally on this many cores (default: all, or the cluster)")
    parser.add_argument("--max-batches", type=int, help="stop after this many batches (for timing runs)")
    parser.add_argument("--driver-memory", default="8g")
    args = parser.parse_args()
    raw, output = args.input.rstrip("/"), args.output.rstrip("/")

    builder = (SparkSession.builder.appName("finqa-text").config("spark.driver.memory", args.driver_memory)
               .config("spark.ui.showConsoleProgress", "false"))
    if args.cores:  # local mode: the Python workers use this same Python environment
        os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
        builder = builder.master(f"local[{args.cores}]")
    spark = builder.getOrCreate()
    partitions = spark.sparkContext.defaultParallelism * 4

    todo = pending_batches(spark, raw, output)[:args.max_batches]
    print(f"{len(todo)} batches to process, {spark.sparkContext.defaultParallelism} cores", flush=True)
    start = time.time()
    for name in todo:
        t = time.time()
        filings, passages = process_batch(spark, raw, output, name, partitions)
        seconds = time.time() - t
        print(f"{name}: {filings} filings, {passages} passages, {seconds:.0f} s, "
              f"{passages / max(seconds, 1):.0f} passages/s", flush=True)
    print(f"total time {time.time() - start:.0f} s")
    spark.stop()


if __name__ == "__main__":
    main()
