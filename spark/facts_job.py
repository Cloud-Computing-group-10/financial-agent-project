"""Spark job: turn the SEC bulk XBRL facts (every filer) into Parquet tables for Postgres.

Locally:
  python spark/facts_job.py --input data/raw --output data/bulk/parquet
On EMR (see docs/CLOUD.md):
  spark-submit --py-files finqa.zip spark/facts_job.py --input s3://BUCKET/companyfacts \\
      --output s3://BUCKET/parquet --tickers s3://BUCKET/company_tickers.json

Input: a folder of companyfacts JSON files, one per company (the unzipped companyfacts.zip).
Spark reads the files in parallel and runs finqa.facts.extract_company() on each one, the same
code the pilot ingest uses. The output is one Parquet dataset per table, plus two small reports
made with DataFrame aggregations: metric coverage and restatement conflicts.
Load the result with:  python -m finqa.load_bulk data/bulk/parquet
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import (BooleanType, DoubleType, LongType, StringType, StructField,
                               StructType)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # run from a checkout
from finqa.facts import extract_company  # noqa: E402

S, L, D, B = StringType(), LongType(), DoubleType(), BooleanType()
SCHEMAS = {  # column order = dict keys from facts.extract_company()
    "companies": [("company_id", L), ("name", S), ("ticker", S), ("name_key", S)],
    "documents": [("accession", S), ("company_id", L), ("form_type", S), ("title", S), ("filed", S),
                  ("period", S), ("period_end", S), ("url", S)],
    "figures": [("accession", S), ("metric", S), ("statement", S), ("tag", S), ("line_label", S),
                ("period", S), ("period_start", S), ("period_end", S), ("duration", S), ("value", D),
                ("unit", S), ("is_derived", B)],
    "metric_tags": [("company_id", L), ("metric", S), ("tags", S), ("unsupported_reason", S)],
}
DATE_COLUMNS = {"filed", "period_start", "period_end"}


def company_rows(text: str, tickers: dict, years: tuple[int, int]):
    """(table, row tuple) pairs for one companyfacts file. Files without 10-Q facts give nothing."""
    data = json.loads(text)
    if not data.get("facts"):
        return
    tables = extract_company(data, ticker=tickers.get(int(data["cik"])), years=years)
    if not tables["documents"]:
        return
    for table, rows in tables.items():
        names = [name for name, _ in SCHEMAS[table]]
        for row in rows:
            yield table, tuple(row[name] for name in names)


def read_tickers(spark: SparkSession, path: str | None) -> dict[int, str]:
    """SEC company_tickers.json: {"0": {"cik_str": 320193, "ticker": "AAPL", ...}, ...}."""
    if not path:
        return {}
    text = "".join(spark.sparkContext.textFile(path).collect())
    return {int(v["cik_str"]): v["ticker"] for v in json.loads(text).values()}


def write_tables(spark: SparkSession, records, output: str) -> dict[str, int]:
    counts = {}
    for table, columns in SCHEMAS.items():
        schema = StructType([StructField(name, kind) for name, kind in columns])
        rows = records.filter(lambda r, t=table: r[0] == t).map(lambda r: r[1])
        df = spark.createDataFrame(rows, schema)
        for name, _ in columns:
            if name in DATE_COLUMNS:
                df = df.withColumn(name, F.to_date(name))
        df.write.mode("overwrite").parquet(f"{output}/{table}")
        counts[table] = spark.read.parquet(f"{output}/{table}").count()
    return counts


def write_reports(spark: SparkSession, output: str) -> None:
    """Coverage per metric, and restatements: the same company, metric and quarter reported
    with different values in different filings (the conflict rule of plan 15.2)."""
    documents = spark.read.parquet(f"{output}/documents")
    figures = spark.read.parquet(f"{output}/figures").join(documents.select("accession", "company_id"), "accession")
    quarters = figures.where(F.col("duration") == "3M")

    coverage = (quarters.groupBy("metric")
                .agg(F.countDistinct("company_id").alias("companies"), F.count("*").alias("figures"),
                     F.round(F.avg(F.col("is_derived").cast("int")), 3).alias("share_derived"))
                .orderBy("metric"))
    conflicts = (quarters.groupBy("company_id", "metric", "period")
                 .agg(F.countDistinct("value").alias("n_values"), F.min("value").alias("min_value"),
                      F.max("value").alias("max_value"))
                 .where(F.col("n_values") > 1))

    coverage.coalesce(1).write.mode("overwrite").option("header", True).csv(f"{output}/report_coverage")
    conflicts.write.mode("overwrite").parquet(f"{output}/report_conflicts")
    print("\nCoverage of quarterly figures per metric:")
    coverage.show(truncate=False)
    print(f"Restatement conflicts (company, metric, quarter with more than one value): {conflicts.count()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, help="folder with companyfacts JSON files (local or s3://)")
    parser.add_argument("--output", required=True, help="folder for the Parquet tables (local or s3://)")
    parser.add_argument("--tickers", help="SEC company_tickers.json, to add tickers to the companies")
    parser.add_argument("--first-year", type=int, default=2019, help="first 10-Q year to keep")
    parser.add_argument("--last-year", type=int, default=2100, help="last 10-Q year to keep")
    args = parser.parse_args()

    start = time.time()
    spark = SparkSession.builder.appName("finqa-facts").getOrCreate()
    tickers = spark.sparkContext.broadcast(read_tickers(spark, args.tickers))
    years = (args.first_year, args.last_year)

    files = spark.sparkContext.wholeTextFiles(f"{args.input.rstrip('/')}/*.json")
    records = files.flatMap(lambda f: company_rows(f[1], tickers.value, years)).cache()
    counts = write_tables(spark, records, args.output)
    write_reports(spark, args.output)

    print(f"rows written: {counts}")
    print(f"files: {files.getNumPartitions()} partitions, total time {time.time() - start:.1f} s")
    spark.stop()


if __name__ == "__main__":
    main()
