"""Static configuration: database, pilot companies, metric list (with XBRL tag candidates), thresholds.

This module has no third-party imports, so the Spark job can ship it to the cluster as-is.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("FINQA_DATA_DIR", ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"  # original SEC files (source of truth); everything in Postgres is rebuilt from them

# Every SEC filer (docs/CLOUD.md). Layout under data/bulk:
#   companyfacts/  submissions.zip  company_tickers.json     SEC bulk downloads
#   parquet/                                                 Spark facts job output
#   text/manifest.csv  text/raw/batch=NNNN/  text/chunks/batch=NNNN/   10-Q text pipeline
BULK_DIR = DATA_DIR / "bulk"
TEXT_DIR = BULK_DIR / "text"

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://finqa:finqa@localhost:5432/finqa")

# SEC asks automated clients to identify themselves. Set your own contact in the environment.
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "cloudcomp-llm-project research-prototype@example.com")

# ---- Pilot companies -------------------------------------------------------------
# The pilot set is ingested from per-company SEC files, including the 10-Q text for "why" questions.
# The Spark job (spark/facts_job.py) adds the numbers of every other SEC filer from the bulk file.
PILOT_COMPANIES = [
    {"ticker": "MMM", "cik": 66740, "name": "3M Company"},
    {"ticker": "KO", "cik": 21344, "name": "The Coca-Cola Company"},
    {"ticker": "JNJ", "cik": 200406, "name": "Johnson & Johnson"},
    {"ticker": "CAT", "cik": 18230, "name": "Caterpillar Inc."},
    {"ticker": "HON", "cik": 773840, "name": "Honeywell International Inc."},
]
# Companies of the evaluation question sets (eval/questions_v2.jsonl): every section of their 10-Qs
# is indexed. For all other filers the bulk text job indexes only MD&A, where the reasons are.
EVAL_COMPANIES = {66740: "MMM", 21344: "KO", 200406: "JNJ", 18230: "CAT", 773840: "HON",
                  77476: "PEP", 1318605: "TSLA", 1800: "ABT", 51143: "IBM", 936468: "LMT"}
MDA_SECTION = "Part I Item 2."  # section label of Management's Discussion and Analysis

PILOT_YEARS = (2022, 2023)  # 10-Qs whose period ends in these years (2022 gives year-over-year comparisons)
TEXT_YEARS = (2023,)        # pilot 10-Qs whose text is indexed for passage search

# Names people use that are not the registered name (lowercase -> ticker).
COMPANY_ALIASES = {
    "coke": "KO",
    "j&j": "JNJ",
    "jnj": "JNJ",
}

# ---- Target metric list -------------------------------------------------------
# field -> ordered candidate XBRL tags, statement, unit kind, user-facing aliases.
METRICS: dict[str, dict] = {
    "net_sales": {
        "label": "Net sales / revenue",
        "tags": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
        "statement": "Income Statement",
        "aliases": ["revenue", "revenues", "net sales", "sales", "net revenue", "net revenues", "turnover", "top line"],
    },
    "cost_of_sales": {
        "label": "Cost of sales",
        "tags": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"],
        "statement": "Income Statement",
        "aliases": ["cost of sales", "cost of goods sold", "cogs", "cost of revenue", "cost of products sold"],
    },
    "gross_profit": {
        "label": "Gross profit",
        "tags": ["GrossProfit"],
        "statement": "Income Statement",
        "aliases": ["gross profit", "gross margin dollars"],
    },
    "sga": {
        "label": "Selling, general & administrative expense",
        "tags": ["SellingGeneralAndAdministrativeExpense"],
        "statement": "Income Statement",
        "aliases": ["sg&a", "sga", "selling general and administrative", "selling, general and administrative", "operating expenses sg&a"],
    },
    "rd_expense": {
        "label": "Research & development expense",
        "tags": ["ResearchAndDevelopmentExpense", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"],
        "statement": "Income Statement",
        "aliases": ["r&d", "research and development", "research & development"],
    },
    "operating_income": {
        "label": "Operating income",
        "tags": ["OperatingIncomeLoss"],
        "statement": "Income Statement",
        "aliases": ["operating income", "operating profit", "operating loss", "ebit", "income from operations"],
    },
    "pretax_income": {
        "label": "Income before income taxes",
        "tags": [
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        ],
        "statement": "Income Statement",
        "aliases": ["pretax income", "pre-tax income", "income before taxes", "earnings before taxes", "profit before tax"],
    },
    "net_income": {
        "label": "Net income attributable to the company",
        "tags": ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss"],
        "statement": "Income Statement",
        "aliases": ["net income", "net earnings", "profit", "net profit", "earnings", "bottom line", "net loss"],
    },
    "eps_diluted": {
        "label": "Diluted earnings per share",
        "tags": ["EarningsPerShareDiluted"],
        "statement": "Income Statement",
        "aliases": ["eps", "diluted eps", "earnings per share", "diluted earnings per share"],
    },
    "operating_cash_flow": {
        "label": "Net cash provided by operating activities",
        "tags": ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
        "statement": "Cash Flow Statement",
        "aliases": ["operating cash flow", "cash from operations", "cash flow from operations", "cash from operating activities", "ocf"],
    },
}

# Tags a company reports that do NOT mean what our field means (found by verifying the answer key
# against the filing documents). The alignment check skips them, so the metric becomes unsupported
# for that company and the system declines instead of answering with the wrong line item.
TAG_EXCLUDE = {  # (CIK, tag) -> reason
    (773840, "OperatingIncomeLoss"):
        "Honeywell has no operating-income line on its income statement; this tag is its "
        "'Total segment profit' from the segment note.",
}

# ---- LLM ------------------------------------------------------------------------
LLM_MODEL = "claude-opus-5-5"  # default for the claude preset; see finqa/llm.py for switching
LLM_EFFORT = os.environ.get("FINQA_EFFORT", "low")  # parsing/explaining are simple tasks

# ---- Search / policies ------------------------------------------------------------
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"  # run locally with fastembed (ONNX, CPU)
EMBED_DIM = 384
TOP_K = 5
MAX_ROUNDS = 2 
MIN_PASSAGES = 2
# Per-method thresholds (to be calibrated on a dev set -- see plan 15.3).
THRESHOLDS = {
    "semantic": {"top": 0.55, "min": 0.40},  # cosine similarity (MiniLM)
    "bm25": {"top": 8.0, "min": 4.0},        # raw BM25 score
    "hybrid": {"top": 0.030, "min": 0.020},  # RRF score
}
