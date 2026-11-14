# Qualitative examples and data sources (numeric evaluation)

These five questions come from `eval/questions_numeric.jsonl`. All answers are from Claude Opus 5.5:
- **Workflow:** re-run on 13 Nov.
- **Baseline:** the saved answers from the evaluation run, shortened here. The full text is in `eval/results/checkpoints/claude-claude-opus-5-5_baseline.jsonl`.

## What "data source accuracy" means and how the source is fetched

**In the workflow the source is never written by the LLM.** The model only returns
`{company, metric, period, compare}`. The number *and* its source then come from one database row:

1. `SQL_LOOKUP` ([finqa/workflow.py](../finqa/workflow.py)) selects the figure. It joins `figures` with
   `documents` (the filing it came from) and `companies`, and prefers the filing that covers that period.
2. Each part of the source comes from that row:

   | Part of the source | Where it comes from |
   |---|---|
   | Filing title + accession number + URL | `documents` row, from SEC's submissions index at ingestion |
   | Statement ("Income Statement" / "Cash Flow Statement") | our metric list in `config.py` (a fixed mapping per metric) |
   | Line item + XBRL tag (e.g. `us-gaap:OperatingIncomeLoss`) | the XBRL fact the value came from, and its standard label |
   | Period ("Q2 2023, three months") | computed from the fact's own start/end dates, not from the filing's fiscal-period field |

3. `figure_locator()` formats these fields into the `[F1]` citation.

**How it is scored** (`score_system` / `score_baseline` in [eval/run_eval.py](run_eval.py)):

- **Workflow:** the cited figure must come from the *correct 10-Q* (its accession number matches the answer key), from the
  *correct statement*, and for the *correct period*. For change questions, the comparison figure must be for the
  prior-year quarter. For the question that must be declined, no figure may be cited.
- **Baseline:** it answers in free text, so the check is that the answer *names* the correct statement (e.g.
  "Statement of Results of Operations", "Statement of Cash Flows") and the correct period ("three months ended
  June 30, 2023", "Q2 2023").
- **The ground truth is the filing itself.** Every value in the answer key was found in the labelled row of the filing's own
  statement table (the `evidence_row` below), by `eval/verify_answer_key.py`.

**Caveats to state honestly:**
- **Statement name:** in the workflow this comes from our fixed mapping, so it is correct by construction once the metric is right. What can actually go wrong is the filing, the period or the tag, and those are what the evaluation catches (see the Honeywell case).
- **Line label:** the workflow shows the XBRL standard label ("Operating Income (Loss)"), not the company's own wording ("Operating profit").
- **Prior-year figure:** the workflow cites it from the prior year's own 10-Q (the filing that covers that period). The baseline cites the comparison column of the current 10-Q. Both are valid; the values are the same.

---

## Example 1: year-over-year change (N18)

**Question:** *How much did Caterpillar's operating profit change year over year in Q2 2023?*

**Answer key:** $3,652M vs $1,944M, +87.86%.
- **Filing row:** `Operating profit | 3,652 | 1,944` in Caterpillar 10-Q Q2 2023 ([0000018230-23-000047](https://www.sec.gov/Archives/edgar/data/18230/000001823023000047/cat-20230630.htm)).

**Workflow:** ✓ correct, ✓ source. 1,064 in / 57 out tokens, 3.7 s.
> Caterpillar Inc.'s operating income for Q2 2023 was $3.652 billion [F1]. That is up 87.9% ($1.708 billion) from $1.944 billion in Q2 2022 [F2] (year-over-year; computed in SQL).
> - [F1] Caterpillar Inc. Form 10-Q for Q2 2023 (0000018230-23-000047) › Income Statement › Operating Income (Loss) [us-gaap:OperatingIncomeLoss] › Q2 2023 (three months)
> - [F2] Caterpillar Inc. Form 10-Q for Q2 2022 (0000018230-22-000183) › Income Statement › Operating Income (Loss) [us-gaap:OperatingIncomeLoss] › Q2 2022 (three months)

**Baseline:** ✓ correct, ✓ source. 84,070 in / 941 out tokens, 10.7 s.
> Caterpillar's operating profit rose $1.708 billion (88%) year over year in Q2 2023, to $3.652 billion from $1.944 billion. Q2 2023 operating profit: $3,652 million (Consolidated Statement of Results of Operations, Operating profit, three months ended June 30, 2023)… *[then MD&A cross-checks and segment detail]*

## Example 2: earnings per share (N20)

**Question:** *How did Caterpillar's diluted earnings per share in Q3 2023 compare to Q3 2022?*

**Answer key:** $5.45 vs $3.87, +40.83%.
- **Filing row:** `Profit per common share – diluted | 5.45 | 3.87` in Caterpillar 10-Q Q3 2023 ([0000018230-23-000056](https://www.sec.gov/Archives/edgar/data/18230/000001823023000056/cat-20230930.htm)).

**Workflow:** ✓ correct, ✓ source. 1,073 in / 59 out tokens, 2.0 s.
> Caterpillar Inc.'s diluted earnings per share for Q3 2023 was $5.45 per share [F1]. That is up 40.8% ($1.58 per share) from $3.87 per share in Q3 2022 [F2].
> - [F1] Caterpillar Inc. Form 10-Q for Q3 2023 (0000018230-23-000056) › Income Statement › Earnings Per Share, Diluted [us-gaap:EarningsPerShareDiluted] › Q3 2023 (three months)

**Baseline:** ✓ correct, ✓ source. 86,025 in / 825 out tokens, 8.2 s.
> Caterpillar's diluted EPS rose to $5.45 in Q3 2023 from $3.87 in Q3 2022, an increase of $1.58, or about 41%. Both figures come from the "Profit per common share – diluted" line of the Consolidated Statement of Results of Operations… *[then an unrequested explanation of why EPS rose]*

*(This is the answer the first grader wrongly marked ✗ because it rounded to "about 41%".)*

## Example 3: cash flow, a negative value (N21)

**Question:** *What was Honeywell's cash from operating activities in Q1 2023?*

**Answer key:** −$784M (cash used).
- **Filing row:** `Net cash provided by (used for) operating activities | (784) | 36` in Honeywell 10-Q Q1 2023 ([0000773840-23-000038](https://www.sec.gov/Archives/edgar/data/773840/000077384023000038/hon-20230331.htm)).

**Workflow:** ✓ correct, ✓ source. 1,061 in / 58 out tokens, 1.9 s.
> Honeywell International Inc.'s net cash provided by operating activities for Q1 2023 was -$784 million [F1].
> - [F1] Honeywell International Inc. Form 10-Q for Q1 2023 (0000773840-23-000038) › Cash Flow Statement › Net Cash Provided by (Used in) Operating Activities [us-gaap:NetCashProvidedByUsedInOperatingActivities] › Q1 2023 (three months)

**Baseline:** ✓ correct, ✓ source. 44,014 in / 946 out tokens, 9.2 s.
> Honeywell's operating activities used $784 million of cash in Q1 2023 (three months ended March 31, 2023)… (Consolidated Statement of Cash Flows, "Net cash provided by (used for) operating activities")… *[then the NARCO payment as the reason]*

*Qualitative note:* the baseline phrases a negative cash flow more naturally ("used $784 million"). The workflow's template says "-$784 million". That is a wording fix for the answer template.

## Example 4: net income with two candidate lines (N22)

**Question:** *How did Honeywell's net income change year-over-year in Q3 2023?*

**Answer key:** $1,514M vs $1,552M, −2.45%.
- **Filing row:** `Net income attributable to Honeywell | 1,514 | 1,552` in Honeywell 10-Q Q3 2023 ([0000773840-23-000085](https://www.sec.gov/Archives/edgar/data/773840/000077384023000085/hon-20230930.htm)).

**Workflow:** ✓ correct, ✓ source. 1,062 in / 55 out tokens, 1.9 s.
> Honeywell International Inc.'s net income attributable to the company for Q3 2023 was $1.514 billion [F1]. That is down 2.5% ($38 million) from $1.552 billion in Q3 2022 [F2].
> - [F1] … Form 10-Q for Q3 2023 (0000773840-23-000085) › Income Statement › Net Income (Loss) Attributable to Parent [us-gaap:NetIncomeLoss] › Q3 2023 (three months)

**Baseline:** ✓ correct, ✓ source. 53,084 in / 1,052 out tokens, 10.1 s.
> Net income was $1,515M, down from $1,549M, a $34M decrease (about 2.2%). Net income attributable to Honeywell was $1,514M, down from $1,552M, a $38M decrease (about 2.4%, my calculation)…

*Qualitative note:* the baseline gives **both** lines, total net income and net income attributable to Honeywell. The
workflow gives the one line our metric list defines (attributable to the company) and labels it as such. Note the
baseline rounds −2.45% to "2.4%", so its percentages are computed by the model, not the database.

## Example 5: a question the system must decline (N24)

**Question:** *What was Honeywell's operating income in Q2 2023?*

**Answer key:** decline. Honeywell's income statement has no operating-income line. The XBRL tag
`OperatingIncomeLoss` is used for its *total segment profit* ($2,052M, segment note of 10-Q Q2 2023,
[0000773840-23-000073](https://www.sec.gov/Archives/edgar/data/773840/000077384023000073/hon-20230630.htm)).

**Workflow:** ✓ declined. 1,057 in / 54 out tokens, 2.2 s.
> Not enough evidence: Honeywell International Inc. does not report operating income in its 10-Q data (Honeywell has no operating-income line on its income statement; this tag is its 'Total segment profit' from the segment note.)

**Baseline:** ✓ declined. 51,703 in / 941 out tokens, 9.3 s.
> The filing does not report "operating income." The Consolidated Statement of Operations has no operating income subtotal… Below are the closest measures the filing does report, plus one calculated proxy. *[table: income before taxes $1,904M, total segment profit, …]*

*Qualitative note:* before the answer key was verified against the filing, the workflow answered this with
"$2,052 million", with a clean-looking citation. **Exact citations do not guarantee correctness when the
underlying tag means something else.** That is why the alignment check now has an exclusion list. Here, the baseline's
reading of the actual document is a real strength.

---

## Summary of the differences

| | Workflow | Baseline |
|---|---|---|
| Where the number comes from | database row (XBRL fact) | the model reading the 10-Q text |
| Where the source comes from | database metadata (accession, tag, period) | the model's own description |
| % change | computed in SQL, exact | computed by the model, rounded |
| Answer length | 1–2 sentences (~55 tokens) | a page with extra context (~760 tokens) |
| Strengths | cheap, fast, exact, works with a small local model | richer context, natural wording, can see when a line doesn't exist |
| Weaknesses | template wording, needs a verified tag mapping | ~80k tokens per question, needs a long-context model |
