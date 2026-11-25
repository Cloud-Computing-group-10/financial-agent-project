"""Shared test helpers: a tiny synthetic companyfacts file and a throwaway test database."""
import os

import psycopg
import pytest

from finqa import db

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://finqa:finqa@localhost:5432/finqa_test")


def fact(start, end, val, accn, filed, form="10-Q"):
    return {"start": start, "end": end, "val": val, "accn": accn, "filed": filed, "form": form}


# Two 10-Qs of a fictional calendar-year company:
#   Q2 2023 filing (A2): revenue for Q2 2023 and Q2 2022, cash flow only as 6-month YTD
#   Q1 2023 filing (A1): revenue for Q1 2023, cash flow for the first 3 months
# Revenue switches tag between the filings, to test the per-filing tag choice.
A1, A2 = "0000000001-23-000001", "0000000001-23-000002"
FACTS = {
    "cik": 1,
    "entityName": "ACME WIDGETS INC",
    "facts": {"us-gaap": {
        "Revenues": {"label": "Revenues", "units": {"USD": [
            fact("2023-04-01", "2023-06-30", 120.0, A2, "2023-08-01"),
            fact("2022-04-01", "2022-06-30", 100.0, A2, "2023-08-01"),
        ]}},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"label": "Revenue from contracts", "units": {"USD": [
            fact("2023-01-01", "2023-03-31", 90.0, A1, "2023-05-01"),
        ]}},
        "NetCashProvidedByUsedInOperatingActivities": {"label": "Operating cash flow", "units": {"USD": [
            fact("2023-01-01", "2023-03-31", 30.0, A1, "2023-05-01"),
            fact("2023-01-01", "2023-06-30", 75.0, A2, "2023-08-01"),
        ]}},
        "EarningsPerShareDiluted": {"label": "Diluted EPS", "units": {"USD/shares": [
            fact("2023-04-01", "2023-06-30", 1.25, A2, "2023-08-01"),
            fact("2022-04-01", "2022-06-30", 1.00, A2, "2023-08-01"),
        ]}},
        # A 10-K fact must be ignored.
        "NetIncomeLoss": {"label": "Net income", "units": {"USD": [
            fact("2022-01-01", "2022-12-31", 400.0, "0000000001-23-000000", "2023-02-01", form="10-K"),
        ]}},
    }},
}


@pytest.fixture
def conn():
    """An empty test database with our schema. Skips the test if Postgres is not running."""
    try:
        admin = psycopg.connect(TEST_DATABASE_URL.rsplit("/", 1)[0] + "/postgres", autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("Postgres is not running (start it with: docker compose up -d db)")
    name = TEST_DATABASE_URL.rsplit("/", 1)[1]
    if not admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
        admin.execute(f"CREATE DATABASE {name}")
    admin.close()

    connection = db.connect(TEST_DATABASE_URL)
    db.init_db(connection, reset=True)
    yield connection
    connection.close()
