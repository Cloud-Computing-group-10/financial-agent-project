# COMP4651: Cloud Computing and Big Data Systems
## Deep Project: Financial Research and Analysis Agent (Route D3)

A robust, traceable financial research assistant that parses SEC 10-Q quarterly filings, routes queries between deterministic SQL lookups and semantic search, and grounds every financial figure and explanation in its exact source document.

---

## Project Structure

```text
comp4651-financial-agent/
├── init_db.py         # SQLite database schema initialization
├── requirements.txt   # Python dependencies
├── README.md          # Project documentation
└── data/              # Storage for raw 10-Q filings and local database (git-ignored)
