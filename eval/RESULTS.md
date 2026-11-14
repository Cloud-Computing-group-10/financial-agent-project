# MVP results — numeric questions (13 Nov 2026)

**Setup**
- **Data:** 5 companies (3M, Coca-Cola, J&J, Caterpillar, Honeywell), their 2023 10-Q filings.
- **Questions:** 25 numeric questions (`eval/questions_numeric.jsonl`), 5 per company over 10 metrics:
  - 15 lookups;
  - 9 year-over-year changes, which need the prior-year value and the % change;
  - 1 question that must be declined (Honeywell does not report operating income).
- **Answer key:** every value checked against the filing's own statement table (`eval/answer_key_numeric.jsonl`, field `evidence_row`).
- **Workflow:** LLM parses the question → SQL template fetches the figure → answer cites filing, statement, line and period.
- **Baseline:** the same LLM gets the whole 10-Q plus the question, with no workflow.

## Results

| | Workflow + Claude Opus 5.5 | Workflow + Qwen2.5-7B (local) | Baseline + Claude Opus 5.5 | Baseline + Qwen2.5-7B |
|---|---|---|---|---|
| **Accuracy** | **100%** (25/25) | **100%** (25/25) | 100% (25/25)* | 0% (0/25), context overflow |
| **Data source accuracy** | **100%** (25/25) | **100%** (25/25) | 100% (25/25)* | 0% (0/25) |
| **Latency** (median / mean / max) | **1.8** / 2.0 / 3.5 s | 1.9 / 2.0 / 4.6 s | 7.3 / 7.8 / 10.7 s | – |
| **Input tokens / question** | 1,063 | **385** | 79,622 | (44k–122k needed, 32k available) |
| **Output tokens / question** | 56 | 45 | 763 | – |
| **Input / output tokens, total** | 26.6k / 1.4k | 9.6k / 1.1k | 1.99M / 19.1k | – |
| **API cost, 25 questions** (approx.) | ≈ $0.13 | $0 (local) | ≈ $8.30 | – |

\* After the run, two grader rules were corrected; the answers were re-scored, not re-run. N20: the baseline
said "about 41%" for a true change of 40.83%, so the % tolerance now follows the answer's stated precision.
N24: the baseline correctly declined ("the filing does not report operating income") and then listed related,
labelled measures, which the old rule wrongly penalised. Under the old rules the baseline scored 23/25
accuracy and 24/25 source accuracy.

## What this shows

1. **Accuracy is the same, the cost is not.** With a strong model, both approaches get the numbers right.
   The workflow does it with **~75× fewer input tokens**, **~14× fewer output tokens**, **~4× lower latency**
   and **~65× lower API cost**, because the model only reads the question and never the 44–122k-token filing.
2. **The workflow makes a small open model usable.** Qwen2.5-7B on a laptop reaches 25/25 inside the workflow
   but cannot run the baseline at all: a 10-Q does not fit its 32k context.
3. **Sources are exact by construction.** The workflow's citations come from database metadata (accession,
   statement, XBRL line, period), not from the model. The baseline's citations are free text and were only
   checked for naming the right statement and period.
4. **Verification caught a real data error.** Honeywell's XBRL "OperatingIncomeLoss" is its *total segment
   profit*; Honeywell reports no operating income. Without the check against the filing, the system would
   have answered $2,052M with a confident citation. Now it declines and says why.

## Where the input tokens go

- **Workflow:** one LLM call per numeric question.
  - Qwen: ~385 tokens. That is the instructions plus the allowed company and metric lists (~280) and a compact JSON shape (~90); the question itself is ~16.
  - Claude: ~1,063 tokens. Our prompt is ~457; the other ~600 are added by the API's structured-output mode, which guarantees valid JSON.
  - Before the cleanup it was ~726 tokens for Qwen. Duplicated field descriptions and a full JSON Schema were removed, and numeric questions no longer trigger an explanation call.
- **Baseline:** the whole filing every time, 44k–122k tokens per 10-Q in Claude's tokenizer.

## Caveats

- 25 questions is a small set; all are drawn from 2023 10-Qs of 5 calendar-year companies.
- Baseline accuracy accepts the filing's own rounding (±0.1%). Its source check is a text match, not exact metadata.
- For 17 of the 25 baseline questions and all workflow questions, only the live scores were kept (the first
  run crashed before saving answer texts); the 8 resumed baseline answers are stored in full in
  `eval/results/checkpoints/`.
