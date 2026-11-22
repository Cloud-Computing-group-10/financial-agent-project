# 10-Q question answering with cited sources

This project answers questions about a company's quarterly results, for example "How much did Caterpillar's operating profit change year over year in Q2 2023?", and every answer says exactly where the number comes from (which filing, which statement, which line, which quarter).

The main idea: the LLM only reads the question and turns it into a few fields (company, metric, period). The number itself is looked up in a database built from the SEC filings, and the source comes from the same database row. So the LLM can't make up a number or a citation.

We compare this "workflow" against baselines: give the same LLM the whole 10-Q filing and the question, and let it answer (plus a simple RAG baseline, see the experiment plan).

The design and the reasons behind it are in `Cloud Computing Project Design Plan.md`; section 15 lists what changed while building it, and section 16 is the experiment plan.


## 1. Architecture

```
QUERY PATH (every question, ~2 s)
  Browser ──▶ Streamlit (frontend/app.py) ──HTTP──▶ FastAPI (api.py) ──▶ finqa.workflow
                                                                            ├─▶ LLM: parse the question (and explain "why")
                                                                            └─▶ Postgres + pgvector: figures (SQL), passages (vectors)

DATA PATH (batch)
  pilot:  SEC files for 5 companies ──▶ python -m finqa.ingest ──▶ Postgres (numbers + 10-Q text)
  bulk numbers: SEC companyfacts.zip (every filer) ──▶ Spark (spark/facts_job.py) ──▶ Parquet ──▶ load_bulk facts ──▶ Postgres
  bulk text:    submissions.zip ──▶ finqa.filings (list + download every 10-Q) ──▶ Spark (spark/text_job.py:
                split + embed) ──▶ Parquet ──▶ load_bulk text ──▶ Postgres
```

Locally everything runs with Docker Compose. In the cloud the same Compose file runs on one large EC2 instance: Spark in local mode on all its cores, Postgres, the API and the frontend, with a copy of the data in S3. See `docs/CLOUD.md`.


## 2. Folder overview

```
api.py                     web API (POST /ask, GET /health)
frontend/app.py            Streamlit page, talks to the API over HTTP
finqa/                     all the system code
  config.py                database URL, pilot companies, metrics + XBRL tags, thresholds
  facts.py                 XBRL facts -> figure rows (period rules, tag choice); used by ingest AND Spark
  db.py                    Postgres schema and bulk loading
  ingest.py                pilot ingestion: download, figures, 10-Q text + embeddings
  filings.py               list of every filer's 10-Qs (from submissions.zip) + resumable download
  load_bulk.py             loads the Spark output (numbers and passages) into Postgres
  embed.py                 passage and question embeddings (MiniLM via fastembed)
  companies.py             finds the company a question is about (ticker, alias, name)
  text.py                  turns 10-Q HTML into sections and passages
  search.py                passage search (keyword / semantic / hybrid)
  llm.py                   all LLM calls (Claude, HKUST gateway, ChatGPT, local Qwen)
  parse.py                 step 1: question -> fields (LLM) + checks
  workflow.py              steps 2-5: lookup, checks, answer with sources
  baseline.py              the baseline: whole 10-Q + question to the LLM
  cli.py                   command line entry point
spark/facts_job.py         Spark job: SEC bulk XBRL file -> figures
spark/text_job.py          Spark job: downloaded 10-Qs -> passages + embeddings
cloud/                     scripts for the EC2 instance (setup, bulk download, whole pipeline, S3 copy)
tests/                     pytest tests (run in GitHub Actions on every push)
eval/                      evaluation (see section 8)
docker-compose.yml         Postgres + API + frontend
Dockerfile                 one image for API, frontend, ingestion and Spark
docs/CLOUD.md              how to run it on AWS (EC2 + S3)
data/raw/                  original SEC files (not in git, downloaded by ingestion)
```


## 3. Setup

You need Docker and Python 3.12.

```bash
cp .env.example .env
```

Put your API key(s) in `.env`: `ANTHROPIC_API_KEY` for Claude (default), `HKUST_API_KEY` for the free HKUST gateway (`--llm hkust`), `OPENAI_API_KEY` for ChatGPT. Also set `SEC_USER_AGENT` to your name and email; SEC asks for this on downloads.

### Run everything in Docker

```bash
docker compose up -d --build
```

```bash
docker compose run --rm api python -m finqa.ingest
```

The second command downloads the SEC files for the 5 pilot companies (about 70 MB, only the first time) and builds the database. Then open http://localhost:8501 for the frontend, or http://localhost:8080/docs for the API. If a port is taken on your machine, set `API_PORT` / `FRONTEND_PORT`, e.g. `API_PORT=8765 docker compose up -d`.

### Run the Python code directly (for development)

Postgres still runs in Docker:

```bash
docker compose up -d db
```

```bash
python3.12 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python -m finqa.ingest
```

```bash
.venv/bin/python -m finqa.cli "What was 3M's revenue in Q2 2023?"
```

Optional extras: `requirements-spark.txt` to run the Spark job locally (needs Java 17), `requirements-local-llm.txt` for local Qwen on Apple Silicon.

### Tests

```bash
.venv/bin/python -m pytest
```

The database tests use a separate database `finqa_test` in the same Postgres, and are skipped if Postgres is not running. GitHub Actions runs all tests and a Docker build on every push (`.github/workflows/ci.yml`).


## 4. The data

- `data/raw/` holds the original files from SEC EDGAR. This is the "source of truth"; everything in Postgres is rebuilt from it.
  - `companyfacts_<TICKER>.json` holds all the structured (XBRL) numbers a company has filed.
  - `submissions_<TICKER>.json` is the list of the company's filings (form type, dates, accession numbers, file names).
  - `<TICKER>/*.htm` is the 10-Q documents themselves, 2023 Q1-Q3 for each company.
- Postgres holds the derived tables (`finqa/db.py`):
  - `companies`: company_id (the SEC CIK), name, ticker, name_key (normalised name for lookups)
  - `documents`: one row per 10-Q: accession, filing date, quarter covered, SEC link, local file (if its text is indexed)
  - `figures`: metric, XBRL tag, line label, period, start/end, duration (3M or YTD), value, unit, is_derived
  - `metric_tags`: which XBRL tags a company uses for each metric, or why the metric is unsupported
  - `chunks`: 10-Q text passages with section, heading and their embedding vector (pgvector)

Pilot companies: 3M (MMM), Coca-Cola (KO), Johnson & Johnson (JNJ), Caterpillar (CAT), Honeywell (HON). These are all calendar-year companies, so Q2 really means April-June. All their 10-Qs from 2022 and 2023 are loaded for numbers (30 filings, 850 figures); the text of the 2023 ones is indexed for search (15 filings, 2,741 passages).

Metrics: net sales/revenue, cost of sales, gross profit, SG&A, R&D, operating income, pretax income, net income, diluted EPS, operating cash flow. They are defined in `finqa/config.py` (`METRICS`).


## 5. Ingestion: how the database is built

### 5.1 Pilot (5 companies, with text)

```bash
.venv/bin/python -m finqa.ingest
```

Add `--offline` to use only the files already in `data/raw`. This resets the database. What it does (`finqa/ingest.py`):

1. `download()` fetches `companyfacts` and `submissions` for each company, and the 10-Q HTML documents for 2023. Files that already exist are skipped.
2. `load_figures()` turns the XBRL facts into rows with `finqa.facts.extract_company()` and writes them with `db.load_facts()`. No LLM and no reading of text. The rules (`finqa/facts.py`):
   - Only 10-Q facts. A fact covering about 3 months is a quarter, e.g. "Q2 2023". 6- and 9-month facts are stored as year-to-date ("6M 2023").
   - The quarter label is computed from the fact's own dates, not from the filing's "fiscal period" field. A Q2 2023 filing also contains Q2 2022 numbers as comparison.
   - Tag choice ("alignment check"): companies use different XBRL tags for the same thing, e.g. revenue can be `Revenues` or `RevenueFromContractWithCustomerExcludingAssessedTax`, and Caterpillar has no `NetIncomeLoss`. Each metric has an ordered list of candidate tags in config.py; **for each filing**, the first candidate tag that filing reports is used, so a company that switches tags between years is still covered. If no candidate is ever reported, the metric is unsupported for that company and `metric_tags` stores why. Tags known to mean something else are skipped (`TAG_EXCLUDE`); right now that is only Honeywell's `OperatingIncomeLoss`, which is really its total segment profit.
   - Cash flow statements in a 10-Q only show year-to-date values, so the quarter is computed as YTD minus the previous YTD and marked `is_derived`.
   - There is no Q4 10-Q (Q4 is only in the annual 10-K), so Q4 questions are declined.
3. `load_text()` reads each 2023 10-Q HTML with `finqa/text.py` (tables become one line per row, sections like "Part I Item 2. MD&A" and a heading path are tracked, text is cut into pieces of about 180 words), embeds each passage with all-MiniLM-L6-v2 (run locally with fastembed) and stores it with its vector in `chunks`.

At the end it prints the counts and the unsupported metrics.

### 5.2 Bulk numbers (every SEC filer) with Spark

SEC publishes `companyfacts.zip` (about 1.4 GB zipped): one companyfacts JSON per company, the same format as the pilot files. `spark/facts_job.py` reads the unzipped folder in parallel, runs the same `extract_company()` on every file, and writes Parquet tables. It also writes two reports with Spark DataFrame aggregations: coverage per metric, and restatement conflicts (the same company, metric and quarter reported with different values in different filings).

```bash
.venv/bin/python spark/facts_job.py --input data/bulk/companyfacts --output data/bulk/parquet --tickers data/bulk/company_tickers.json
```

```bash
.venv/bin/python -m finqa.load_bulk facts data/bulk/parquet
```

`load_bulk` skips companies already in the database. You can try the job on the pilot files: `--input data/raw --first-year 2022 --last-year 2023` gives the same 850 figures as the pilot ingest. Add `--cores N` to use N cores (default: all).

### 5.3 Bulk text (every filer's 10-Qs) with Spark

For "why" questions about any company, the 10-Q documents themselves are needed:

1. `python -m finqa.filings manifest` lists every 10-Q in the documents table that has no text yet, and finds the file name of its main document in SEC's `submissions.zip`. The list is split into batches of 2,000 filings (`data/bulk/text/manifest.csv`).
2. `python -m finqa.filings download` downloads them, 8 requests per second (SEC allows 10), gzipped into `data/bulk/text/raw/batch=NNNN/`. A batch is finished when its `_DONE` file exists, so a stopped download continues where it left off.
3. `python spark/text_job.py --input data/bulk/text/raw --output data/bulk/text/chunks` splits every finished batch with `finqa/text.py` and embeds the passages with `finqa/embed.py` (the same code as the pilot ingest), one Spark task per core. Batches already done are skipped.
4. `python -m finqa.load_bulk text data/bulk/text/chunks` adds the passages to Postgres, batch by batch; filings that already have text are skipped.

`cloud/run_pipeline.sh` runs all of 5.2 and 5.3 in order, with steps 3 and 4 repeating while the download runs.


## 6. Asking a question: what happens step by step

Command:

```bash
.venv/bin/python -m finqa.cli "How much did Caterpillar's operating profit change year over year in Q2 2023?"
```

Options:
- `--llm qwen` uses the local model instead of Claude
- `--json` prints the full result including the trace
- `--baseline` runs the baseline instead of the workflow

Example output:

```
Caterpillar Inc.'s operating income for Q2 2023 was $3.652 billion [F1]. That is up 87.9% ($1.708 billion) from $1.944 billion in Q2 2022 [F2] (year-over-year; computed in SQL).
Sources:
  [F1] Caterpillar Inc. Form 10-Q for Q2 2023 (0000018230-23-000047) › Income Statement › Operating Income (Loss) [us-gaap:OperatingIncomeLoss] › Q2 2023 (three months)
  [F2] Caterpillar Inc. Form 10-Q for Q2 2022 (0000018230-22-000183) › Income Statement › Operating Income (Loss) [us-gaap:OperatingIncomeLoss] › Q2 2022 (three months)
[llm=claude:claude-opus-5-5 status=answered latency=3.74s llm_calls=1 tokens=1064+57]
```

The steps, all in `answer()` in `finqa/workflow.py`:

Step 1: parse the question (`finqa/parse.py`, `parse_question()`). One LLM call. The prompt is `_SYSTEM` in parse.py: it lists the allowed metrics and asks for JSON with company (copied as written in the question), metric, period, qtype (numeric / explanatory / both), compare (none / yoy / qoq) and unsupported_reason. The question is the user message.

For the CAT example the LLM returns:

```
{"company": "Caterpillar", "metric": "operating_income", "period": "Q2 2023", "qtype": "numeric", "compare": "yoy", "unsupported_reason": null}
```

Then code checks the result (this is not a second parser, just validation):
- the company must actually be written in the question; if the LLM returns a company that isn't, it is removed, so the system asks "which company?"
- the company mention is looked up in the database (`finqa/companies.py`): first known aliases ("Coke", "J&J"), then tickers, then the normalised name, then names that start with it ("Honeywell" -> "Honeywell International"). No match: declined. Several matches: the system asks which one.
- the metric must be in the metric list
- out-of-scope questions are caught by fixed rules (`scope_violation()`): annual/10-K, Q4, ratios/margins, segment numbers
- if there is no "why" word in the question, it is treated as numeric

If the LLM call fails (no key, no credits, network), the answer is status "error". There is no fallback.

Step 2: route. Numeric questions go to the lookup; "why" questions go to text search (not in the current evaluation); "both" does both.

Step 3: lookup (`numeric_path()`). The LLM never writes SQL. There are two fixed SQL templates at the top of workflow.py:
- `SQL_LOOKUP` finds the figure for (company, metric, period, 3-month). It joins figures with documents so the filing comes along. If several filings have the same number (current value and later comparison column), the filing that covers that quarter is preferred.
- `SQL_CHANGE` computes the absolute and percentage change between two figure rows, in SQL.

For "yoy" the second period is the same quarter one year earlier; for "qoq" it is the previous quarter (`shift_period()`).

Step 4: checks.
- No figure found: declined ("Not enough evidence ..."). If the metric is unsupported for that company, the reason from `metric_tags` is given, e.g. Honeywell operating income.
- Two filings report different values for the same quarter (a restatement): both are shown and flagged.
- No period in the question: the latest quarter is used and the answer says so ("Assumptions: ...").

Step 5: answer (`render_numeric()` + `figure_locator()`). The sentence is a fixed template filled with the numbers. Each number gets a citation [F1], [F2] built from the database row: filing title, accession, statement, line label, XBRL tag, period.

Possible statuses: answered, abstain (declined), clarify (asks back), partial, error.

### "Why" questions (code exists, not evaluated right now)

For a "why" question, `retrieve()` searches the passages of that one filing. It searches the MD&A section first and then the whole filing with a rewritten query, at most 2 rounds. If the best score is below a threshold (`THRESHOLDS` in config.py), it declines and shows the closest passages. Otherwise `compose_explanation()` sends the question, the figures and the top 5 passages (tagged [C<id>]) to the LLM with the prompt `_EXPLAIN_SYSTEM`. The LLM must return claims, each with the passage ids it comes from. Code drops any claim that cites an id that wasn't retrieved.

Search methods (`finqa/search.py`): `bm25` (keyword), `semantic` (embeddings, cosine similarity), `hybrid` (both combined with reciprocal rank fusion). Choose with `--method`.


## 7. The LLM layer and switching models

All LLM calls go through `finqa/llm.py`:
- `parse()` asks for structured JSON that is validated against a Pydantic model; it is used by the parser and the explainer.
- `text()` is free text, used by the baseline.

Choose the model with `--llm` (and optionally `--model`) on the CLI and the eval script:

| --llm | runs on | default model |
|---|---|---|
| claude (default) | Anthropic API | claude-opus-5-5 |
| hkust | HKUST GenAI gateway (key in HKUST_API_KEY, free for students) | gpt-4o-mini |
| chatgpt | OpenAI API (key in OPENAI_API_KEY) | gpt-4.1 |
| qwen / mlx | your Mac (mlx-lm) | mlx-community/Qwen2.5-7B-Instruct-4bit |
| qwen-14b | your Mac (mlx-lm) | mlx-community/Qwen2.5-14B-Instruct-4bit |
| ollama / openai | an OpenAI-compatible server | qwen2.5:7b-instruct |

Example: `--llm claude --model claude-sonnet-5-5`. You can also set `FINQA_LLM` and `FINQA_MODEL` as environment variables.

Differences between the backends:
- Claude uses the API's structured outputs, so the JSON is always valid. This adds about 600 input tokens per call on the API side.
- ChatGPT, Ollama and other OpenAI-compatible servers use JSON mode plus the same validation as the local models (next point).
- Local models get a short JSON "shape" in the prompt. Their reply is validated with the same Pydantic model, and if it is invalid the model gets one retry showing its error.
- Local models have a 32k token context (`FINQA_LOCAL_CONTEXT`). The baseline refuses to run when the filing doesn't fit, instead of cutting the filing.


## 8. Evaluation

Everything is in `eval/`:

```
eval/
  questions_numeric.jsonl      the 25 numeric questions (current evaluation)
  answer_key_numeric.jsonl     correct answers, each verified against the filing
  verify_answer_key.py         builds + verifies the answer key
  run_eval.py                  runs workflow and baseline, scores, writes results
  RESULTS.md                   results write-up for the slides
  EXAMPLES.md                  side-by-side example answers and sources
  questions_all_types.jsonl    old mixed set (explanatory + edge cases), for later
  results/                     output files of every run
    checkpoints/               saved per-question results (resume support)
    old_mixed_set/             results from the earlier mixed-question runs
```

### 8.1 The questions

There are 25 questions, 5 per company, spread over 10 metrics and Q1-Q3 2023:
- 15 are simple lookups ("What was 3M's revenue in Q2 2023?").
- 9 ask for a year-over-year change ("How much did Caterpillar's operating profit change year over year in Q2 2023?"). These need the current value, the prior-year value and the % change.
- 1 must be declined: N24, "What was Honeywell's operating income in Q2 2023?". Honeywell doesn't report operating income.

The questions use different wording on purpose ("top line", "profit", "cash from operating activities", "net operating revenues") to test that the LLM maps them to the right metric.

Quarter-over-quarter and derived cash-flow questions are left out because the baseline only gets one filing, and those values aren't in it, so the comparison would be unfair.

### 8.2 The answer key and how it is verified

Command:

```bash
.venv/bin/python eval/verify_answer_key.py
```

What it does:
1. For each question it drafts the correct answer from the database (value, prior-year value, % change, the filing's accession number, statement, period).
2. Then it checks that answer against the filing document itself, not the database. It opens the 10-Q HTML, goes through the table rows, and looks for a row whose label fits the metric (e.g. "sales" or "revenue" for net sales) and that contains the value as printed in the filing (in millions, e.g. "8,325", negative values in parentheses). For change questions the same row must also contain the prior-year value, i.e. current and comparison columns of the same line item.
3. A value is only marked `verified: true` if such a row is found. The matching row is saved as `evidence_row`, e.g. "Operating profit | 3,652 | 1,944".

This is a real check, because the XBRL data and the HTML tables are two different renderings of the filing. It found one real error: Honeywell's `OperatingIncomeLoss` tag is its total segment profit ($2,052M), not operating income. Without this check the system would have answered with a wrong number and a perfect-looking citation. The tag is now excluded and N24 expects a decline.

All 119 stored quarterly figures (not only the 25 in the questions) were checked the same way; all match.

Run this again whenever the questions or the data change. `run_eval.py` warns if the key has unverified rows.

### 8.3 Running the evaluation

Command (workflow + baseline with Claude):

```bash
.venv/bin/python eval/run_eval.py --llm claude
```

Same with local Qwen:

```bash
.venv/bin/python eval/run_eval.py --llm qwen
```

Options:
- `--only system` or `--only baseline` runs just one side.
- `--fresh` ignores saved checkpoints and reruns everything. With Claude that costs about $8 again.
- `--model <id>` uses a different model.

What it does for each question:
- **Workflow:** calls `answer(question)`, the same code as the CLI.
- **Baseline:** calls `baseline_answer(question, company, period)`. It gets the correct filing for that question, so it is a generous baseline. The prompt is `INSTRUCTION` in `finqa/baseline.py`: answer only from the filing, cite the statement/section, line item and period for every number, and say so if the evidence isn't there. The whole 10-Q is converted to plain text and sent with the question. For Claude that is 44k to 122k tokens per filing.
- **Scoring:** the answer is scored and the result is saved right away to `eval/results/checkpoints/<model>_<side>.jsonl`. If the run stops (out of credits, network error), run the same command again and it continues where it stopped; already answered questions are not paid again.

Output files in `eval/results/`:
- `numeric_<model>_<time>.json` has the summary plus every question with answer, scores, tokens and LLM steps.
- `numeric_<model>_<time>.csv` has one row per question per side (good for charts).
- `numeric_<model>_<time>.md` has the summary table.

### 8.4 The four metrics and how they are scored

All scoring is in `eval/run_eval.py` (`score_system()` for the workflow, `score_baseline()` for the baseline).

**Accuracy: is the number right?**
- Workflow: the returned figure must equal the answer key exactly. For change questions the prior-year figure must also be right and the % change within 0.05 points. For N24 the status must be "abstain".
- Baseline: free text, so the answer must contain the correct value. It may be written in millions or billions, but only rounding of up to 0.1% is accepted, which matches the filing's own precision. For change questions it must also contain the prior-year value and the % change, correct to the precision it states ("41%" for 40.83% is fine, "40.6%" is not). For N24 it must say up front that the figure isn't reported.
- A baseline that couldn't run (context overflow) counts as wrong.

**Data source accuracy: is the cited source the right one?**
- Workflow: the citation comes from the database row, not from the LLM. It must have the correct filing (accession number from the answer key), the correct statement and the correct quarter. For change questions the second figure must be the prior-year quarter. For N24 no figure may be cited.
- Baseline: free text, so the answer must name the correct statement (e.g. "Statement of Results of Operations", "Statement of Cash Flows") and the correct period ("three months ended June 30, 2023", "Q2 2023"). This is looser than the workflow check.
- Note: in the workflow the statement name comes from our fixed metric list, so it is correct once the metric is right. The things that can really go wrong are the filing, the quarter and the tag, and those are what this catches.

**Latency:** wall-clock seconds per question, including the LLM call(s) and the database. Median, mean and max are reported.

**Input/output tokens:** the LLM tokens per question (mean) and in total, as reported by the API (Claude) or counted by the tokenizer (Qwen). For the workflow it is one LLM call per numeric question.

### 8.5 Results (13 Nov 2026, 25 questions)

| | Workflow + Claude | Workflow + Qwen (local) | Baseline + Claude | Baseline + Qwen |
|---|---|---|---|---|
| Accuracy | 25/25 | 25/25 | 25/25 | 0/25 (did not fit) |
| Data source accuracy | 25/25 | 25/25 | 25/25 | 0/25 |
| Latency, median | 1.8 s | 1.9 s | 7.3 s | - |
| Input tokens per question | 1,063 | 385 | 79,622 | - |
| Output tokens per question | 56 | 45 | 763 | - |
| API cost for 25 questions | about $0.13 | $0 | about $8.30 | - |

In short: with Claude both are fully accurate, but the workflow needs about 75x fewer input tokens and is about 4x faster. Qwen-7B on a laptop gets 25/25 in the workflow but can't run the baseline at all, because a 10-Q doesn't fit in its 32k context.

Two baseline grader rules were corrected after the Claude run (rounding of % and how a decline is detected). The saved answers were re-scored, not re-run. Before the fix the baseline had 23/25. Details are in `eval/RESULTS.md`; example answers are in `eval/EXAMPLES.md`.

Where the workflow's input tokens go:
- The question itself is about 16 tokens. The rest is the fixed prompt: instructions plus the metric list, and the JSON shape, about 90. (In the MVP run the prompt also listed the 5 companies; since the company lookup moved to the database it no longer does, and Haiku now uses about 1,000 input tokens per question.)
- For Claude, the API's structured output adds about 600 more.
- The prompt used to be about 726 tokens for Qwen. It was cut by removing duplicated descriptions and the full JSON Schema, and by not running the explanation step for numeric questions.

### 8.6 Retrieval experiment (for "why" questions, not part of the MVP)

This compares the three search methods on the 12 "why" questions in `questions_all_types.jsonl`. Questions, data and filing filter are the same; only the search method changes. A hit means a passage containing the known evidence phrase is in the top k.

```bash
.venv/bin/python eval/run_eval.py retrieval
```

It is not used for numeric questions because nothing is searched there: the lookup is exact (company, metric, period), and its correctness is what data source accuracy measures. Last result: BM25 hit@3 0.75, semantic 0.42, hybrid 0.50; hybrid has the best hit@1 (0.50). With 12 questions this is not conclusive.


## 9. Web API

The API is what the frontend and any other client use:

```bash
curl -s localhost:8080/ask -H 'content-type: application/json' -d '{"question": "What was 3M revenue in Q2 2023?", "llm": "claude"}'
```

Fields: `question`, `method` (semantic / bm25 / hybrid), `llm` (a preset), `model` (optional model id). `GET /health` shows the configured LLM and the presets. Interactive docs are at `/docs`. Inside Docker, local Qwen via mlx doesn't work (Linux, no Apple GPU); use Claude, HKUST or an Ollama server there.


## 10. Limitations and known issues

- Small evaluation set: 5 companies, 2023 10-Qs, 25 numeric questions. The bulk load adds the numbers of every filer, but only the pilot companies have indexed text and a verified answer key.
- Quarters are calendar quarters of the period end. For companies with a non-calendar fiscal year (e.g. NVIDIA), "Q2 2026" means April-June 2026, not their fiscal Q2.
- Only consolidated figures, no segments. No annual (10-K) data, so no Q4.
- Only the 10 metrics in config.py. Ratios and margins are declined.
- The tag mapping is only verified against the filing documents for the pilot companies (see the Honeywell case). For bulk companies it is automatic and unverified.
- Answer wording is a template. For example, a negative cash flow reads "-$784 million" instead of "used $784 million".
- The workflow shows the XBRL standard label ("Operating Income (Loss)"), not the company's own line name ("Operating profit").
- The baseline source check is a text match, not exact like the workflow's.
- For 17 of the 25 Claude baseline answers and all Claude workflow answers, only the scores were kept from the first run; the text was lost in a crash. Newer runs save the full answers.
- The "why" path is built but not evaluated well yet: its search thresholds are not calibrated and its explanation scoring is a simple phrase match.


## 11. Troubleshooting

- `llm.available()` is False or the API answers "Could not resolve authentication method": the API key is missing. Put it in `.env`; inside Docker, the containers only see `.env`, not your shell.
- "credit balance is too low" means the Anthropic account has no credits. Top up, then rerun the same eval command (it resumes).
- `connection refused` on port 5432: Postgres is not running. Start it with `docker compose up -d db`.
- A port is already taken when starting Docker Compose: set `API_PORT`, `FRONTEND_PORT` (e.g. `API_PORT=8765 docker compose up -d`).
- Qwen baseline shows "context_overflow". That is expected: a 10-Q doesn't fit into 32k tokens.
