# 10-Q question answering with cited sources

This project answers questions about a company's quarterly results, for example "How much did Caterpillar's operating profit change year over year in Q2 2023?", and every answer says exactly where the number comes from (which filing, which statement, which line, which quarter).

The main idea: the LLM only reads the question and turns it into a few fields (company, metric, period). The number itself is looked up in a database built from the SEC filings, and the source comes from the same database row. So the LLM can't make up a number or a citation.

We compare this "workflow" against a simple baseline: give the same LLM the whole 10-Q filing and the question, and let it answer.

The design and the reasons behind it are in `Cloud Computing Project Design Plan.md`; section 15 lists what changed while building it.

Current scope (MVP): numeric questions only. The code for "why" questions (text search plus an LLM explanation) is still there but not part of the current evaluation.


## 1. Folder overview

```
project_llm/
  README.md                          this file
  Cloud Computing Project Design Plan.md
  requirements.txt                   python packages (with pinned versions)
  api.py                             small web API (POST /ask)
  Dockerfile                         container for the web API
  finqa/                             all the system code
    config.py                        companies, metrics, XBRL tags, settings
    db.py                            database tables
    ingest.py                        downloads SEC data and builds the database
    text.py                          turns 10-Q HTML into text sections and chunks
    search.py                        text search (keyword / semantic / hybrid)
    llm.py                           all LLM calls (Claude or local Qwen)
    parse.py                         step 1: question -> fields (LLM) + checks
    workflow.py                      steps 2-5: lookup, checks, answer with sources
    baseline.py                      the baseline: whole 10-Q + question to the LLM
    cli.py                           command line entry point
  data/                              built data (see section 3)
  eval/                              evaluation (see section 7)
```


## 2. Setup

You need Python 3.12. On a Mac with Apple Silicon you can also run the local Qwen model.

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

The versions in requirements.txt are pinned on purpose:
- torch 2.3.1, transformers 4.51.3 and numpy below 2 have to match. transformers 5 refuses to use torch 2.3, which breaks the text search.
- mlx-lm 0.24.1 is the last version that works with transformers 4.51. It is only needed for local Qwen.

### Claude (default LLM)

Set your Anthropic API key in the shell. To keep it, add this line to `~/.zshrc`, then open a new terminal or run `source ~/.zshrc`:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Check that the code sees it (should print True):

```bash
.venv/bin/python -c "from finqa import llm; print(llm.available())"
```

### ChatGPT instead of Claude (optional)

Put your OpenAI API key in the shell (or in `~/.zshrc`, like the Anthropic key):

```bash
export OPENAI_API_KEY="sk-..."
```

Then add `--llm chatgpt` to any command. The default model is gpt-4.1; pick another with `--model`:

```bash
.venv/bin/python -m finqa.cli "What was 3M's revenue in Q2 2023?" --llm chatgpt
```

```bash
.venv/bin/python -m finqa.cli "What was 3M's revenue in Q2 2023?" --llm chatgpt --model gpt-4.1-mini
```

```bash
.venv/bin/python eval/run_eval.py --llm chatgpt
```

From Python:

```python
from finqa import llm
from finqa.workflow import answer, render

llm.configure("chatgpt", "gpt-4.1")      # or llm.configure("claude"), llm.configure("qwen")
res = answer("How much did Caterpillar's operating profit change year over year in Q2 2023?")
print(render(res))                        # answer + [F1]/[F2] sources + tokens/latency
```

How it works: no extra package is needed. ChatGPT goes through the same "OpenAI-compatible" code that Ollama uses (`_openai_generate()` in `finqa/llm.py`). It sends a plain HTTP request to `https://api.openai.com/v1/chat/completions`. The core of it is:

```python
import os, requests

body = {
    "model": "gpt-4.1",
    "messages": [{"role": "system", "content": system_prompt},
                 {"role": "user", "content": question}],
    "max_tokens": 1000,
    "temperature": 0,
    "response_format": {"type": "json_object"},   # only for the parse step
}
r = requests.post("https://api.openai.com/v1/chat/completions", json=body,
                  headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}, timeout=600)
reply = r.json()["choices"][0]["message"]["content"]
tokens_in, tokens_out = r.json()["usage"]["prompt_tokens"], r.json()["usage"]["completion_tokens"]
```

Some OpenAI models (the reasoning ones) refuse `max_tokens` or `temperature`. The code sees the 400 error and retries with `max_completion_tokens` and without `temperature`, so any model name works. The JSON reply of the parse step is checked with the same Pydantic model as for Claude, with one retry if it is invalid. The preset settings (URL, key variable, context size 1M for gpt-4.1, output cap) are in `ENDPOINTS` at the top of `finqa/llm.py`. If you pick a model with a smaller context, set `FINQA_CHATGPT_CONTEXT` so the baseline refuses filings that don't fit instead of failing.

Not tested yet against the real OpenAI API (no OpenAI key here). It was tested end to end against a fake server that behaves like the OpenAI API.

### Local Qwen (optional, free, runs on your Mac)

Uses mlx-lm (installed by requirements.txt on macOS). The model (Qwen2.5-7B-Instruct, 4-bit, about 4.3 GB) is downloaded the first time you use `--llm qwen`. After that it works offline.

You can also use Ollama instead: install Ollama, run `ollama pull qwen2.5:7b-instruct`, then use `--llm ollama`.


## 3. The data

Everything is in `data/`:

- `data/raw/` is the original files from SEC EDGAR, about 68 MB. This is the "source of truth"; everything else is built from it.
  - `companyfacts_<TICKER>.json` holds all the structured (XBRL) numbers a company has filed.
  - `submissions_<TICKER>.json` is the list of the company's filings (form type, dates, accession numbers, file names).
  - `<TICKER>/*.htm` is the 10-Q documents themselves (the readable filing), 2023 Q1-Q3 for each company.
- `data/finqa.db` is the SQLite database built from the raw files.
- `data/embeddings.npy` + `embeddings_ids.json` are the vectors for the text search.
- `data/alignment_report.json` shows which XBRL tag was chosen for each company and metric, and which metrics are not available.

Companies: 3M (MMM), Coca-Cola (KO), Johnson & Johnson (JNJ), Caterpillar (CAT), Honeywell (HON). These are all calendar-year companies, so Q2 really means April-June.

Filings: all 10-Qs from 2022 and 2023 are in the database for their numbers (30 filings). The text of the 2023 ones is indexed for search (15 filings). The 2022 filings are there so year-over-year questions have the prior-year numbers.

Metrics: net sales/revenue, cost of sales, gross profit, SG&A, R&D, operating income, pretax income, net income, diluted EPS, operating cash flow. They are defined in `finqa/config.py` (`METRICS`).


## 4. Ingestion: how the database is built

Command, which rebuilds everything from the files already in `data/raw` without downloading:

```bash
.venv/bin/python -m finqa.ingest --offline
```

Without `--offline` it first downloads anything missing from SEC (about 70 MB on a fresh machine). SEC wants a contact in the User-Agent, so set `SEC_USER_AGENT="Your Name your@email"` first.

What `finqa/ingest.py` does, in order:

1. `download()` fetches `companyfacts` and `submissions` for each company, and the 10-Q HTML documents for 2023. Files that already exist are skipped.

2. `alignment_check()` is the "pre-ingest alignment check". Companies use different XBRL tags for the same thing. For example, revenue can be `Revenues` or `RevenueFromContractWithCustomerExcludingAssessedTax`, and Caterpillar has no `NetIncomeLoss` at all. For each metric `config.py` has a list of candidate tags, and the first one the company actually reports in 2023 10-Qs is chosen. If none exists, the metric is marked unsupported for that company. Some tags are known to mean something else and are skipped (`TAG_EXCLUDE` in config.py). Right now that is only Honeywell's `OperatingIncomeLoss`, which is really its total segment profit; Honeywell has no operating income line. The result is written to `data/alignment_report.json`.

3. The documents table gets one row per 10-Q: company, form type, filing date, the quarter it covers, accession number, SEC URL, and the local file path.

4. Figures (layer 1) come straight from the XBRL facts, with no LLM and no reading of text. For each chosen tag, every 10-Q fact is stored with its value, unit, start/end dates and the filing it came from. Period rules (`period_of()`):
   - A fact covering about 3 months is a quarter, e.g. "Q2 2023". 6- and 9-month facts are stored as year-to-date ("6M 2023").
   - The quarter label is computed from the fact's own dates, not from the filing's "fiscal period" field. A Q2 2023 filing also contains Q2 2022 numbers as comparison.
   - Cash flow statements in a 10-Q only show year-to-date values. `_derive_quarters_from_ytd()` computes the quarter as YTD minus the previous YTD and marks it `is_derived`.
   - There is no Q4 10-Q (Q4 is only in the annual 10-K), so Q4 questions are declined.

5. Chunks (layer 2) come from `finqa/text.py`, which reads each 2023 10-Q HTML:
   - `html_to_lines()` turns tables into one line per row ("Net sales | 8,325 | 8,702 | ...") and removes page furniture like "Table of Contents".
   - `sectionize()` tracks Part I/II and Item 1/2/3, e.g. "Part I Item 2. MD&A", plus a heading path like "AEROSPACE › Q2 2023 compared to Q2 2022".
   - `chunk()` cuts the text into pieces of about 180 words.

   Duplicate chunks are dropped.

6. `build_index()` in `finqa/search.py` turns every chunk into a vector with the small embedding model all-MiniLM-L6-v2, which runs locally and is loaded from the Hugging Face cache only.

At the end it prints how many figures and chunks were stored (currently 840 figures, 2741 chunks) and which metrics are unsupported.

The database tables (`finqa/db.py`):
- `companies`: company_id, name, ticker, cik
- `documents`: report_id, company_id, title, form_type, date, period, period_end, accession, url, file_path, has_text
- `figures`: figure_id, report_id, statement, metric, tag, line_label, period, period_start, period_end, duration (3M or YTD), value, unit, is_derived
- `chunks`: chunk_id, report_id, section, heading, ordinal, text


## 5. Asking a question: what happens step by step

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

Step 1: parse the question (`finqa/parse.py`, `parse_question()`). One LLM call. The prompt is `_SYSTEM` in parse.py: it lists the allowed companies and metrics and asks for JSON with company, metric, period, qtype (numeric / explanatory / both), compare (none / yoy / qoq) and unsupported_reason. The question is the user message.

For the CAT example the LLM returns:

```
{"company": "CAT", "metric": "operating_income", "period": "Q2 2023", "qtype": "numeric", "compare": "yoy", "unsupported_reason": null}
```

Then code checks the result (this is not a second parser, just validation):
- the company must actually be named in the question (`find_company()`); if the LLM guessed a company that isn't named, it is removed, so the system asks "which company?"
- the metric must be in the metric list
- out-of-scope questions are caught by fixed rules (`scope_violation()`): annual/10-K, Q4, ratios/margins, segment numbers
- if there is no "why" word in the question, it is treated as numeric

If the LLM call fails (no key, no credits, network), the answer is status "error". There is no fallback.

Step 2: route. Numeric questions go to the lookup; "why" questions go to text search (not in the current evaluation); "both" does both.

Step 3: lookup (`numeric_path()`). The LLM never writes SQL. There are two fixed SQL templates at the top of workflow.py:
- `SQL_LOOKUP` finds the figure for (ticker, metric, period, 3-month). It joins figures with documents so the filing comes along. If several filings have the same number (current value and later comparison column), the filing that covers that quarter is preferred.
- `SQL_CHANGE` computes the absolute and percentage change between two figure rows, in SQL.

For "yoy" the second period is the same quarter one year earlier; for "qoq" it is the previous quarter (`shift_period()`).

Step 4: checks.
- No figure found: declined ("Not enough evidence ..."). If the metric is unsupported for that company, the reason from the alignment report is given, e.g. Honeywell operating income.
- Two filings report different values for the same quarter (a restatement): both are shown and flagged.
- No period in the question: the latest quarter is used and the answer says so ("Assumptions: ...").

Step 5: answer (`render_numeric()` + `figure_locator()`). The sentence is a fixed template filled with the numbers. Each number gets a citation [F1], [F2] built from the database row: filing title, accession, statement, line label, XBRL tag, period.

Possible statuses: answered, abstain (declined), clarify (asks back), partial, error.

### "Why" questions (code exists, not evaluated right now)

For a "why" question, `retrieve()` searches the passages of that one filing. It searches the MD&A section first and then the whole filing with a rewritten query, at most 2 rounds. If the best score is below a threshold (`THRESHOLDS` in config.py), it declines and shows the closest passages. Otherwise `compose_explanation()` sends the question, the figures and the top 5 passages (tagged [C<id>]) to the LLM with the prompt `_EXPLAIN_SYSTEM`. The LLM must return claims, each with the passage ids it comes from. Code drops any claim that cites an id that wasn't retrieved.

Search methods (`finqa/search.py`): `bm25` (keyword), `semantic` (embeddings, cosine similarity), `hybrid` (both combined with reciprocal rank fusion). Choose with `--method`.


## 6. The LLM layer and switching models

All LLM calls go through `finqa/llm.py`:
- `parse()` asks for structured JSON that is validated against a Pydantic model; it is used by the parser and the explainer.
- `text()` is free text, used by the baseline.

Choose the model with `--llm` (and optionally `--model`) on the CLI and the eval script:

| --llm | runs on | default model |
|---|---|---|
| claude (default) | Anthropic API | claude-opus-5-5 |
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


## 7. Evaluation

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

### 7.1 The questions

There are 25 questions, 5 per company, spread over 10 metrics and Q1-Q3 2023:
- 15 are simple lookups ("What was 3M's revenue in Q2 2023?").
- 9 ask for a year-over-year change ("How much did Caterpillar's operating profit change year over year in Q2 2023?"). These need the current value, the prior-year value and the % change.
- 1 must be declined: N24, "What was Honeywell's operating income in Q2 2023?". Honeywell doesn't report operating income.

The questions use different wording on purpose ("top line", "profit", "cash from operating activities", "net operating revenues") to test that the LLM maps them to the right metric.

Quarter-over-quarter and derived cash-flow questions are left out because the baseline only gets one filing, and those values aren't in it, so the comparison would be unfair.

### 7.2 The answer key and how it is verified

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

### 7.3 Running the evaluation

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

### 7.4 The four metrics and how they are scored

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

### 7.5 Results (13 Nov 2026, 25 questions)

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
- The question itself is about 16 tokens. The rest is the fixed prompt: instructions plus the company and metric lists, about 280 tokens, and the JSON shape, about 90.
- For Claude, the API's structured output adds about 600 more.
- The prompt used to be about 726 tokens for Qwen. It was cut by removing duplicated descriptions and the full JSON Schema, and by not running the explanation step for numeric questions.

### 7.6 Retrieval experiment (for "why" questions, not part of the MVP)

This compares the three search methods on the 12 "why" questions in `questions_all_types.jsonl`. Questions, data and filing filter are the same; only the search method changes. A hit means a passage containing the known evidence phrase is in the top k.

```bash
.venv/bin/python eval/run_eval.py retrieval
```

It is not used for numeric questions because nothing is searched there: the lookup is exact (company, metric, period), and its correctness is what data source accuracy measures. Last result: BM25 hit@3 0.75, semantic 0.42, hybrid 0.50; hybrid has the best hit@1 (0.50). With 12 questions this is not conclusive.


## 8. Web API and Docker (optional)

Start the API locally:

```bash
.venv/bin/uvicorn api:app --port 8080
```

Then send a question:

```bash
curl -s localhost:8080/ask -H 'content-type: application/json' -d '{"question": "What was 3M revenue in Q2 2023?", "llm": "claude"}'
```

`GET /health` shows which LLM is configured.

The Dockerfile packages the API, the database and the search index into a container, so it can be deployed to a cloud container service (Cloud Run, ECS, ...). This is the possible "cloud" part of the project; check the course requirements. Inside a container local Qwen via mlx doesn't work (Linux, no Apple GPU); use Claude or an Ollama server there.

```bash
docker build -t finqa .
```

```bash
docker run -p 8080:8080 -e ANTHROPIC_API_KEY=... finqa
```


## 9. Limitations and known issues

- Small data set: 5 companies, 2023 10-Qs, 25 numeric questions.
- Only consolidated figures, no segments. No annual (10-K) data, so no Q4.
- Only the 10 metrics in config.py. Ratios and margins are declined.
- The tag mapping must be verified per company (see the Honeywell case). New companies need the verify step.
- Answer wording is a template. For example, a negative cash flow reads "-$784 million" instead of "used $784 million".
- The workflow shows the XBRL standard label ("Operating Income (Loss)"), not the company's own line name ("Operating profit").
- The baseline source check is a text match, not exact like the workflow's.
- For 17 of the 25 Claude baseline answers and all Claude workflow answers, only the scores were kept from the first run; the text was lost in a crash. Newer runs save the full answers.
- The "why" path is built but not evaluated well yet: its search thresholds are not calibrated and its explanation scoring is a simple phrase match.


## 10. Troubleshooting

- `llm.available()` prints False. The API key isn't in this shell: run `source ~/.zshrc` or open a new terminal.
- "credit balance is too low" means the Anthropic account has no credits. Top up, then rerun the same eval command (it resumes).
- "PyTorch >= 2.6 is required" or the search breaks after installing something: transformers was upgraded. Reinstall the pinned versions from requirements.txt.
- numpy errors with torch: numpy 2 got installed. Use `pip install "numpy<2"`.
- Embedding model not found: it loads from the local Hugging Face cache only. Allow one download with `FINQA_ALLOW_DOWNLOAD=1`.
- Qwen baseline shows "context_overflow". That is expected: a 10-Q doesn't fit into 32k tokens.
