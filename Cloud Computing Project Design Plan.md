# **Project Design Summary**

## **1\. Project type & scope**

1. This is a Deep Project, which means it must be our own workflow or component; simply comparing models or wrapping an API is not sufficient. (This is a project requirement, not a design choice.)  
2. Scope (financial research example): answer natural-language questions about a company's earnings and how they change, gathering evidence, performing calculations, and tracing conclusions to sources.  
3. Supported question types are limited to two simple things: a numeric lookup or a simple percentage change, and a "why" explanation. Derived or ratio metrics (e.g. DPO) are out of scope.

Scope statement (one sentence):

> Given a question about a company's earnings or a change in them, the system identifies the company, metric, and period, retrieves the supporting evidence, computes any numbers, and returns an answer where every figure is traced to a source — or it asks for clarification / abstains.

## **2\. Question types**

| Type | Example | Path |
| :---- | :---- | :---- |
| Numeric / factual | "What was Q2 revenue?" or "How much did it change vs last year?" | Exact lookup / simple calculation (SQL) |
| Explanatory / "why" | "Why did last quarter drop?" | Passage search \+ cited numbers |

Rule of thumb: "what/when/how much" \= numeric; "why/how" \= explanatory.

## **3\. Workflow (5 steps)**

1. Parse question into company, metric, and period (if unclear, ask; if no period is given, assume the latest and state that assumption).  
2. Route the question: numeric goes to lookup; explanatory goes to search.  
3. Retrieve text or compute numbers.  
4. Check for conflicts and missing evidence.  
5. Output a cited answer, or abstain.

   ## **4\. Design principle**

1. The LLM handles language and reasoning; deterministic code handles numbers and citations.  
2. Numbers come from fixed rows in the database, not from model guessing; source tags come from the retrieval tool's metadata, not the model.

   ## **5\. Data source**

1. Use SEC quarterly filings (10-Q) only. Quarterly reports are much smaller than annual ones, so they fit within the model's context window, and they match a "last quarter" scope. *(v2: filings are fetched from SEC EDGAR as HTML/iXBRL, not PDF — see section 15.)*  
2. Each filing contains both things we need: structured, machine-readable figures and narrative explanations (the MD\&A section, which explains why results changed).  
3. Practical example used: 3M Form 10-Q, period ended June 30, 2023\.  
4. Limitation: annual 10-K filings are out of scope; results may differ on them.

   ## **6\. Data pipeline (two layers)**

Ingestion reads each filing once and produces two derived outputs, while keeping the original file.

1. Layer 1 — Structured facts: each fact carries a tag name, a value, a period, and a unit. A script reads these directly, with no LLM and no text reading, into the figures table.  
2. Before ingesting, we define our target metric list and verify that each chosen report actually uses those tags, then map each tag to our field (the pre-ingest alignment check).  
3. Layer 2 — Text: the document body (especially MD\&A) is split into chunks; each chunk is stored with its source page in a search index.  
4. The original files (PDF/HTML) are kept on disk as the source of truth. The figures table and the search index are derived and can be rebuilt from the originals at any time.

   ## **7\. Storage (three stores)**

1. Original files on disk — the readable source, referenced from the documents table.  
2. SQL database (the relational schema):  
3. `companies` (company\_id, name, ticker)  
4. `documents` (report\_id, company\_id, title, form\_type, date, url, file\_path)  
5. `figures` (figure\_id, report\_id, statement, metric, period, value, unit, page)  
6. Search index — not SQL. It stores text chunks with their source page and their vector, so a query can return the exact passage that answers a question.

Notes:

1. A figure's location is identified by: report \+ statement \+ line-item label \+ period. This is the locator; `page` is stored as best-effort but is not required.  
2. For version 1 we store only whole-company (consolidated) figures. A `scope`/`dimension` column for segments is noted as a future schema change.  
3. No junction table is needed: a company has many documents, but each document belongs to one company (one-to-many).  
4. Store the unit explicitly (e.g. "USD millions") so scaled numbers cannot be confused.  
5. Store the period consistently (e.g. "Q2 2023") so a point in time is never confused with a span.  
6. Index the common filters (company, period); this is an assumption to verify later against real question logs.

   ## **8\. Search**

1. Use semantic search, which matches meaning rather than exact words, so it handles paraphrased questions.  
2. The keyword-only method is kept as a controlled-experiment baseline: the same questions are run with only the search method changed, to test whether the extra vector step is worth it.

   ## **9\. Policies & stopping rules**

1. Clarify if incomplete: missing or unmappable company/metric means we ask, rather than guess.  
2. Assume period if absent: use the latest report and state the assumption.  
3. Search stopping: stop when the top relevance score is at or above the threshold and enough passages are found, or after a maximum number of rounds, whichever comes first.  
4. Abstain when weak: if the best score is still below the threshold after the maximum rounds, return "not enough evidence" plus the closest passages.  
5. Conflict rule: if two figures disagree, prefer the higher-priority (primary filing) source; if still unresolved, show both and flag it.  
6. Output rule: every number and every explanatory claim carries a source (report \+ statement \+ line/period).

Answer-quality rule for explanatory questions: an answer counts as good enough only if it (a) states the correct reason and (b) cites the correct source (report \+ statement). This is our checkable rubric.

The tool returns a relevance score (does the passage match the query); deciding whether it answers the question is the LLM's job. Confidence must come from the tool's score and an existence check, not the model's self-report.

## **10\. Numeric path**

1. Metric alias list: map user wording to a fixed field (e.g. "revenue" maps to net\_sales).  
2. Parameterised query templates, written once so the LLM only fills in company/period and never writes SQL freely:  
3. Single metric, single period (exact lookup).  
4. Metric change vs prior period (fetch both periods, compute the percentage).  
5. Store only raw values and compute percentage changes in SQL rather than storing them.  
6. Derived or ratio metrics are out of scope; future work could add them.

   ## **11\. Explanatory path**

1. Route "why/how" questions to passage search.  
2. Restrict the search to the relevant report(s) via the documents table.  
3. Retrieve the most relevant passages; each returns report and page metadata.  
4. The LLM explains using only the retrieved passages and attaches a source to each claim.  
5. Combine with figures from the figures table where needed.  
6. If the evidence is weak, abstain.

   ## **12\. Evaluation**

1. Baseline (generous): a plain LLM given the full correct 10-Q document, with no tools or workflow, and the same instruction to cite sources. A stronger baseline makes any win by our system more credible and reflects how the task would be done without our workflow.  
2. Comparison scope: system-level (accuracy and cost), not single-variable isolation; isolating a single variable is an ablation, which is optional extra work.  
3. Test set: a mixed set across 3 categories — numeric, explanatory, and edge cases (vague or insufficient evidence, which should trigger abstention). Keep it balanced and do not over-split by difficulty. Suggested default: about 5 companies x 5 questions.  
4. Metrics:  
   1. Comparable (both systems, same instruction): answer accuracy, grounding/citation correctness, latency, token cost.  
   2. System-only (our workflow): abstention quality, routing correctness, template correctness.  
   3. Explanatory answers are scored against the rubric in section 9 (correct reason \+ correct source).  
   4. Controlled experiment (required): semantic search vs keyword-only, run on the same questions and same data with only the search method changed. This isolates a single variable and provides the controlled comparison the project requires.  
   5. Limitations: annual 10-K filings are out of scope; the baseline is limited by the context window on larger documents.  
   6. Required evidence in the report: a focused challenge, at least one baseline, controlled experiments, suitable measures, and a limitations section.

   ## **13\. Key Design Decisions & Justifications**

1. A dedicated workflow (routing, templates, policies) rather than a single prompt: this is our own contribution and the reason the project qualifies as a Deep Project.  
2. LLM for language, deterministic code for numbers and citations: guarantees exact figures and real sources while still allowing natural explanations.  
3. Fixed, normalised schema as a single source of truth: makes queries predictable and keeps one authoritative value per figure; a join table is unnecessary because the company-to-document relationship is one-to-many.  
4. Scripted extraction from structured tags with automatic self-checks: the script re-verifies itself using the statement's own totals and cross-period presence, so there is no manual bottleneck and the query path stays deterministic.  
5. Locator as report \+ statement \+ line \+ period instead of a page number: page numbers are unreliable for tagged facts, whereas the statement and line always identify a figure.  
6. Semantic search, tested against a keyword-only alternative in a controlled experiment: paraphrase questions are a known weakness of keyword search, so changing only the search method lets us measure whether the extra vector complexity earns its place.  
7. Generous baseline with an identical instruction, plus separate comparable and system-only metrics: makes the head-to-head fair and honest while still documenting our own system's internal quality.

   ## **14\. Build & submission order**

Track A — Design (due 8 November)

1. Write the design plan (this document).  
2. Turn the design plan into presentation slides.  
3. Submit the slides by 8 November. No implementation is required at this stage; the slides present the plan only.

Track B — Build & experiment (due 1 December) 4\. Pick 10-Q reports and download the PDFs. 5\. Run the pre-ingest tag alignment check, then build the data layer (schema, ingestion, extraction, automatic checks). 6\. Build the answer key (questions \+ answers \+ source locators, across the 3 categories). 7\. Build the baseline. 8\. Build the workflow (routing, templates, search, policies). 9\. Run both systems on the same questions with the same instruction. 10\. Score (accuracy, grounding, latency, cost). 11\. Write the report and submit the report plus source code by 1 December.

## **15\. Changes in v2 (after first feasibility review, 12 November)**

These tweaks keep the design above intact; they fix practical problems found while checking the data and turn vague rules into implementable ones. Sections above are unchanged except where marked *(v2)*.

### 15.1 Data source & ingestion

1. **No PDFs.** 10-Qs are filed as HTML with inline XBRL. Structured facts come from the SEC EDGAR `companyfacts` API (`data.sec.gov/api/xbrl/companyfacts/CIK##########.json`), and the filing text from the filing's primary HTML document (found via the `submissions` API). Track B step 4 becomes "pick 10-Qs and download their facts + primary documents". The raw JSON/HTML files are kept on disk as the source of truth.
2. **Period handling rules (XBRL traps):**
   1. A 10-Q reports both a 3-month and a year-to-date value for the same tag. A fact is a *quarter* only if its duration is about 3 months (80–100 days); 6- and 9-month facts are stored as YTD (`6M 2023`, `9M 2023`).
   2. The `fy`/`fp` fields in the API belong to the *filing*, not the fact (a Q2 2023 filing also carries Q2 2022 comparatives). The period label is computed from the fact's own start/end dates.
   3. Cash-flow statements in a 10-Q are YTD only. A quarterly cash-flow value is derived as YTD(n) − YTD(n−1) and stored with `is_derived = 1`.
   4. There is no Q4 10-Q (Q4 is only in the 10-K, which is out of scope). Q4 questions are answered with an abstention that explains why.
3. **Per-company tag alignment.** The metric list maps each field to an ordered list of candidate XBRL tags (e.g. `net_sales` → `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`, `SalesRevenueNet`). The pre-ingest check picks the first tag that each company actually reports and writes an alignment report; a metric with no matching tag is marked unsupported for that company (and the system asks or abstains instead of guessing). Checking the five pilot companies confirmed this is needed: e.g. Caterpillar has no `NetIncomeLoss` tag.
4. **Citation locator for text.** HTML filings have no reliable page numbers, so text chunks are cited by report + section (e.g. "Item 2. MD&A") + nearest heading + chunk id. This matches the figure locator idea in section 7 (report + statement + line + period).

### 15.2 Storage *(additions to section 7)*

1. `documents` adds `accession` and `period` (the fiscal quarter the filing covers).
2. `figures` adds `tag` (XBRL concept), `line_label`, `period_start`, `period_end`, `duration` (`3M`/`YTD`) and `is_derived`.
3. New `chunks` table (chunk_id, report_id, section, heading, ordinal, text). Chunks are stored in SQLite; vectors are stored next to the database as a NumPy matrix (one row per chunk). This is the "search index"; at this scale (≈ 15 filings) a dedicated vector database is unnecessary, and it can be swapped later.
4. **Conflict rule made concrete:** the same period's value can appear in several filings (as a current value and as a later comparative). The value from the filing that *covers* that period is primary; if a later filing reports a different value (a restatement), both are shown and the answer is flagged.

### 15.3 Search & stopping rules *(refines sections 8–9)*

1. **Keyword baseline = BM25**, not naive keyword matching, so the controlled experiment compares semantic search against a strong keyword method. A hybrid (reciprocal-rank fusion of both) is also implemented as an optional third arm.
2. **Embeddings run locally** (`BAAI/bge-small-en-v1.5` via `fastembed`, CPU only): no API cost, and results can be reproduced.
3. **Rounds:** at most 2. Round 1 uses the question; round 2 runs only if round 1 is weak, using a rewritten query (metric name + change words). Stop as soon as the top score ≥ threshold and at least 2 passages are above the minimum score.
4. **Thresholds are calibrated on a small dev set** of questions that is separate from the test set, never tuned on the test set. Raw similarity scores are not probabilities, so the threshold is a tuned number per search method.
5. **Mixed questions** ("how much did revenue drop *and why*") follow both paths: the figures come from the numeric path and the explanation from the explanatory path.

### 15.4 LLM usage *(refines section 4)*

1. Model: Claude (`claude-opus-5-5`) through the official Anthropic Python SDK. It is used in exactly three places: (a) parsing the question into a validated schema (company, metric, period, question type), (b) writing the explanation from the retrieved passages, and (c) the baseline.
2. The parser always returns structured JSON validated against a schema, and its output is then checked by code (the company must exist in `companies` and the metric must be in the alias list), so the LLM cannot invent fields.
3. Explanations are returned as a list of claims, each with chunk ids. Code verifies that every cited id was actually retrieved and drops claims with invalid citations.
4. **Switchable model (added 13 Nov):** the LLM is a switch (`--llm claude|qwen|qwen-14b|ollama`, `--model <id>`). Open-source Qwen2.5 runs locally (mlx-lm on Apple Silicon, or any OpenAI-compatible server such as Ollama). Local models get the JSON schema in the prompt plus Pydantic validation with one repair attempt instead of server-side enforcement. This allows an extra comparison: *closed vs open model inside the same workflow*, and *the same open model in the workflow vs in the full-document baseline*. The baseline with a 32k-context model cannot fit a ~55k-token 10-Q; this is recorded as `context_overflow` (not truncated) and is itself evidence for the workflow approach.
5. ~~Offline fallback~~ *(removed 13 Nov)*: the rule-based parser and extractive explainer were dropped, so there is one workflow and it always uses an LLM. If the LLM call fails, the answer is reported as an error. Deterministic **checks** on the LLM's parse remain: the company must be named in the question, the out-of-scope rules (10-K, Q4, ratios, segments) always apply, and a question without a "why" word is never sent to the explanation path.

### 15.9 MVP slide focus *(13 Nov, group decision)*

1. **Evaluate numeric questions only for now.** The set is 25 questions (5 per company, lookups + year-over-year changes, 10 metrics), plus one that must be declined. Quarter-over-quarter and derived cash-flow questions are left out because the baseline only gets one filing, which doesn't contain those values.
2. **Every answer-key value is verified against the filing document**, not only against the structured data: the value (and the prior-year value) must appear in the labelled row of the filing's own statement table. This audit found one real data error: Honeywell's `OperatingIncomeLoss` tag is its *total segment profit*, since Honeywell reports no operating income. The tag is now excluded (`TAG_EXCLUDE`), and the system declines that question.
3. **Metrics presented:** latency, input/output tokens, accuracy, and data source accuracy (the cited filing, statement and period are correct).
4. **Token budget:** the parse prompt was cut from ~726 to ~381 tokens per question for Qwen. The cuts were removing duplicated field descriptions, using a compact JSON shape instead of a full JSON Schema, and not running the explanation call for numeric questions. For Claude, about 600 of the ~1,050 tokens per call are added by the API's structured-output mode, which guarantees valid JSON.

### 15.5 Evaluation *(refines section 12)*

1. Test set grows from 25 to **about 50–60 questions** (5 companies × ~10–12), with deliberately paraphrased explanatory questions. With only 25 questions, the semantic-vs-keyword difference would not be distinguishable from noise. If time runs short, keep 25 and state the lack of statistical power under limitations.
2. **Retrieval-level metric for the controlled experiment:** for each explanatory question, the answer key lists evidence phrases that a correct passage must contain. Hit@k / MRR are computed for BM25 vs semantic vs hybrid, which needs no LLM and no grading by hand.
3. Numeric answer-key values are drafted from the structured data and **checked by hand against the filing** (otherwise the system would be graded against its own source).
4. Edge cases cover: unknown company, unsupported metric, Q4 / 10-K questions, missing period (assumption stated), and "why" questions with no supporting passage.

### 15.6 Cloud component *(new — to confirm against course requirements)*

The plan above has no cloud element, but the course is Cloud Computing. The prototype therefore includes a small HTTP API (FastAPI, `POST /ask`) and a Dockerfile, so the system can be deployed as a container (e.g. Cloud Run / App Service / ECS) with the data directory baked in or mounted. **To do:** check what the project brief requires (deployment, scaling, managed storage) and extend this section.

### 15.7 Pilot data set

Five calendar-year companies, all 2023 10-Qs (Q1–Q3): 3M (MMM), Coca-Cola (KO), Johnson & Johnson (JNJ), Caterpillar (CAT), Honeywell (HON). Figures from 2022 10-Qs are also loaded so year-over-year changes and restatement conflicts can be checked.

### 15.8 MVP status (prototype for the interim hand-off)

The MVP implements: ingestion + alignment check, the SQLite stores, BM25/semantic/hybrid search, the full 5-step workflow with policies, the baseline, an evaluation harness with a starter question set, a CLI and an HTTP API. Still to do: a full hand-verified answer key, calibrating thresholds on a dev set, the complete evaluation run with the LLM, and the written report.