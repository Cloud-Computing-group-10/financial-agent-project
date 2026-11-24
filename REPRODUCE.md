# Reproducing the system and every experiment

This file lists every command needed to rebuild the system from nothing and rerun every reported
experiment, in order. It also says where each command runs, how long it takes, what it costs, and
which numbers you should get. The *why* behind the design is in `README.md` and
`Cloud Computing Project Design Plan.md`; the AWS console steps are in more detail in `docs/CLOUD.md`.

Three places run commands. Each step says which one:

| Label | Machine | What runs there |
|---|---|---|
| **[laptop]** | your computer (we used an Apple Silicon Mac) | FinanceBench PDF loading, Qwen via mlx, grading, summary |
| **[ec2]** | the EC2 instance, in `~/financial-agent-project` | the bulk pipeline, Postgres, Ollama, load test, scaling |
| **[aws]** | the AWS web console | launching, stopping and resizing the instance |

You only need the laptop for the quick check (step 1). The full database (9,695 companies) needs the
EC2 instance.


## Overview: time and cost

| Step | Where | Wall-clock time | Cost |
|---|---|---|---|
| 1. Quick check (5 companies) | laptop | 15 min | ~$0.01 API |
| 2. Launch and prepare the instance | aws, ec2 | 30 min | – |
| 3. Bulk database (every SEC filer since 2019) | ec2 | ~9 h (unattended) | ~$13 EC2 |
| 4. FinanceBench filings | laptop → ec2 database | ~40 min | – |
| 5. Answer key | ec2 | ~5 min | – |
| 6. E1 accuracy runs | laptop (+ ec2 for Ollama) | ~6 h in all, mostly Qwen | < $1 API |
| 7. Grading and grader check | laptop | ~15 min | < $0.20 API |
| 8. E2 retrieval, E3 latency, E4 load, E5 scaling | ec2 / laptop | ~1 h | ~$2 EC2 |
| 9. Summary tables | laptop | seconds | – |

What we spent in total: about $19 of EC2 (c7i.8xlarge plus disk) and under $1 of Anthropic API.


## 0. Prerequisites

Accounts:
- **AWS**: a normal account (we used one with $100 credit, not Learner Lab). It must be on the
  *Paid plan* and have a vCPU quota of at least 32 for "Running On-Demand Standard instances"
  (`docs/CLOUD.md` section 0).
- **Anthropic API key** for Claude Haiku 5.5 (the model under test and the grader). About $1 of
  credit is enough for every run.
- An email address for SEC's `User-Agent` header (SEC requires a contact on automated downloads).

On the laptop:
- Docker (Docker Desktop on macOS), Python 3.12, git.
- For local Qwen: an Apple Silicon Mac with at least 16 GB of memory.

The versions we used are listed at the end of this file.


## 1. Quick check on a laptop (5 companies, no cloud)

This builds a small database (3M, Coca-Cola, J&J, Caterpillar, Honeywell; 2022-2023 10-Qs) and
answers a question. It is the fastest way to check that the code works.

**[laptop]** Clone the repository and create the settings file:

```bash
git clone https://github.com/Cloud-Computing-group-10/financial-agent-project.git
```

```bash
cd financial-agent-project && git checkout feature/cloud-app && cp .env.example .env
```

Open `.env` and fill in `ANTHROPIC_API_KEY` and `SEC_USER_AGENT` (your name and email). `.env` is
ignored by git; never commit it.

**[laptop]** Start Postgres, the API and the frontend, then build the pilot database (it downloads about 70 MB from SEC):

```bash
docker compose up -d --build
```

```bash
docker compose run --rm api python -m finqa.ingest
```

Open http://localhost:8501 and ask "What was 3M's revenue in Q2 2023?". The answer should be
$8,325 million, citing the Q2 2023 10-Q. (API docs: http://localhost:8080/docs.)

**[laptop]** For development and for the experiments, also install the Python environment:

```bash
python3.12 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/pip install -r requirements-local-llm.txt
```

The last one is only for local Qwen on a Mac (mlx-lm).

**[laptop]** Run the tests (35 tests; the database tests use a separate `finqa_test` database and are skipped without Postgres):

```bash
.venv/bin/python -m pytest
```

**[laptop]** Ask from the command line, with Claude or with local Qwen:

```bash
.venv/bin/python -m finqa.cli "Why did Caterpillar's sales increase in Q2 2023?"
```

```bash
.venv/bin/python -m finqa.cli "What was 3M's revenue in Q2 2023?" --llm qwen-3b
```

The first Qwen call downloads the model from Hugging Face (about 2 GB for 3B, 4.5 GB for 7B).


## 2. Launch and prepare the EC2 instance

**[aws]** EC2 → Launch instance (details and screenshots of the options in `docs/CLOUD.md` section 2):
- Region `us-east-1`, name `finqa`, image **Ubuntu 26.04 LTS** (x86_64)
- Type **c7i.8xlarge** (32 vCPU, 64 GB, ~$1.43/h)
- Key pair `finqa` (RSA, .pem), saved as `~/.ssh/finqa.pem`
- Security group: SSH (22) and TCP 8501 from **My IP** only
- Storage: **250 GB gp3**
- Optional: IAM instance profile `finqa-ec2` with S3 access, for the S3 copy in step 3

**[laptop]** Protect the key, then copy the code to the instance (the repository is private, so we copy the checkout instead of cloning). Replace `<host>` with the instance's public DNS name:

```bash
chmod 400 ~/.ssh/finqa.pem
```

```bash
cd .. && rsync -av --exclude .venv --exclude .git --exclude data/bulk --exclude data/financebench/pdfs --exclude .env --exclude eval/results/archive -e "ssh -i ~/.ssh/finqa.pem" financial-agent-project ubuntu@<host>:~/ && cd financial-agent-project
```

```bash
ssh -i ~/.ssh/finqa.pem ubuntu@<host>
```

**[ec2]** Install Docker, Compose and the AWS CLI, then log out and in again (so your user may run `docker`):

```bash
cd financial-agent-project && bash cloud/ec2_setup.sh && exit
```

**[ec2]** Create `.env` with at least `SEC_USER_AGENT` (the API key is not needed on the instance; all Claude calls ran from the laptop), then build the image (about 5 minutes, 2.5 GB):

```bash
cd financial-agent-project && cp .env.example .env && nano .env
```

```bash
docker compose build
```


## 3. Build the database for every SEC filer (~9 hours)

**[ec2]** Download SEC's bulk files (companyfacts.zip, submissions.zip, ticker list; about 3 GB, a few minutes):

```bash
bash cloud/fetch_bulk.sh
```

**[ec2]** Run the whole pipeline in the background. It survives logging out, and if it stops, running it again skips finished work:

```bash
nohup bash cloud/run_pipeline.sh > data/bulk/pipeline.log 2>&1 &
```

**[ec2]** Follow it:

```bash
tail -f data/bulk/pipeline.log data/bulk/download.log
```

What the script does (`cloud/run_pipeline.sh`), with our timings on c7i.8xlarge:

| Step | Command inside the script | Time |
|---|---|---|
| 1. Spark facts job: XBRL JSON → Parquet | `spark/facts_job.py` | 40 s |
| 2. Load figures into Postgres | `python -m finqa.load_bulk facts` | ~1 min |
| 3. List every 10-Q since 2019 | `python -m finqa.filings manifest` | ~2 min |
| 4. Download the 10-Qs (8 requests/s, SEC's limit is 10) | `python -m finqa.filings download` | ~5 h |
| 5. Spark text job: MD&A → passages + embeddings, per finished batch | `spark/text_job.py` | ~8 h, alongside 4 |
| 6. Load passages into Postgres | `python -m finqa.load_bulk text` | minutes per batch |

Options: `CORES=8` (fewer Spark cores), `FIRST_YEAR=2021` (fewer years, less download).

**[ec2]** Check the result when the log says `done`:

```bash
docker compose exec db psql -U finqa -c "SELECT (SELECT count(*) FROM companies) companies, (SELECT count(*) FROM documents) documents, (SELECT count(*) FROM figures) figures, (SELECT count(*) FROM chunks) chunks, (SELECT count(*) FROM documents WHERE has_text) with_text"
```

Expected (data as of 23 November 2026): 9,695 companies, 140,436 documents, 3,022,763 figures,
about 4.5 million chunks, 101,734 filings with text. SEC adds filings every day, so a later run
gives somewhat larger numbers.

**[ec2]** Optional: copy the data lake to S3 (needs the instance role):

```bash
bash cloud/backup_to_s3.sh <your-bucket>
```


## 4. FinanceBench filings (~40 minutes)

The 84 FinanceBench PDFs are parsed and embedded on the laptop (faster per core) and written into
the instance's database through an SSH tunnel. The question files are already in
`data/financebench/` (from github.com/patronus-ai/financebench, CC-BY-NC 4.0).

**[laptop]** Open the tunnel (local port 5433 → Postgres on the instance; port 5432 is never open to the internet):

```bash
ssh -i ~/.ssh/finqa.pem -f -N -L 5433:localhost:5432 ubuntu@<host>
```

**[laptop]** Point every later laptop command at the cloud database, for this terminal:

```bash
export DATABASE_URL=postgresql://finqa:finqa@localhost:5433/finqa
```

**[laptop]** Download the PDFs (about 160 MB), then load them (documents + 43,351 page passages):

```bash
.venv/bin/python -m finqa.pdf_docs --download
```

```bash
.venv/bin/python -m finqa.pdf_docs
```

Both commands skip what is already done.


## 5. Answer key of our question set (~5 minutes)

`eval/answer_key_v2.jsonl` is committed, so you can skip this step. To rebuild and re-verify it
against the filings:

**[ec2]**

```bash
docker compose run --rm -T api python eval/build_answer_key.py
```

It must report 60/60 verified. Two answers depend on the date: M01 and M02 ask about "the latest
quarter", which was Q3 2026 when we ran it. On a later date they move to a newer quarter.


## 6. E1: accuracy of every system and model

All runs append one line per answer to `eval/results/<set>/<system>__<model>.jsonl`. A stopped run
continues where it stopped, without paying twice. The commands below run on the laptop against the
cloud database (tunnel from step 4 open, `DATABASE_URL` exported).

Systems: **A** our workflow, **A0** the workflow with template answers (no writer LLM), **C** simple
RAG over the same filing, **N** closed book (only the question, no retrieval). Models: `--llm claude --model claude-haiku-5-5`, `--llm qwen` (Qwen2.5-7B
via mlx), `--llm qwen-3b` (Qwen2.5-3B via mlx).

**[laptop]** Our question set, test split (50 questions), all 9 combinations:

```bash
for s in A A0 C; do .venv/bin/python eval/run_experiments.py run --set v2 --split test --system $s --llm claude --model claude-haiku-5-5; done
```

```bash
for m in qwen qwen-3b; do for s in A A0 C; do .venv/bin/python eval/run_experiments.py run --set v2 --split test --system $s --llm $m; done; done
```

**[laptop]** FinanceBench with Haiku, all 150 questions:

```bash
for s in A C; do .venv/bin/python eval/run_experiments.py run --set financebench --system $s --llm claude --model claude-haiku-5-5; done
```

**[laptop]** FinanceBench with Qwen, on a fixed random sample of 50 (seed 0, the same questions for every system):

```bash
for m in qwen qwen-3b; do for s in A C; do .venv/bin/python eval/run_experiments.py run --set financebench --system $s --llm $m --sample 50; done; done
```

**[ec2]** Qwen2.5-3B on the instance's CPU via Ollama (system A, our test set; this run also gives the E3 step timings):

```bash
docker compose --profile ollama up -d ollama && docker compose exec ollama ollama pull qwen2.5:3b-instruct
```

```bash
docker compose run --rm -T api python eval/run_experiments.py run --set v2 --split test --system A --llm ollama-3b
```

```bash
docker compose --profile ollama stop ollama
```

Then copy the result file to the laptop so it is graded with the others:

**[laptop]**

```bash
rsync -av -e "ssh -i ~/.ssh/finqa.pem" "ubuntu@<host>:financial-agent-project/eval/results/v2/A__openai_qwen2.5_3b-instruct.jsonl" eval/results/v2/
```

**[laptop]** Closed-book baseline N (added after the main runs). It needs no database, so neither the instance nor the tunnel. Our test set with all three models, then FinanceBench (Haiku on all 150, Qwen on the same 50-question sample):

```bash
.venv/bin/python eval/run_experiments.py run --set v2 --split test --system N --llm claude --model claude-haiku-5-5
```

```bash
for m in qwen qwen-3b; do .venv/bin/python eval/run_experiments.py run --set v2 --split test --system N --llm $m; done
```

```bash
.venv/bin/python eval/run_experiments.py run --set financebench --system N --llm claude --model claude-haiku-5-5
```

```bash
for m in qwen qwen-3b; do .venv/bin/python eval/run_experiments.py run --set financebench --system N --llm $m --sample 50; done
```

N costs a few cents with Haiku (about 100 input tokens per question) and takes well under an hour
for both Qwen models. Then grade (step 7) and summarize (step 9) as for the other systems.

Rough run times: Haiku 10-30 minutes per combination; Qwen on the Mac 30-60 minutes per combination
(most of it LLM time, plus the tunnel); Ollama on the CPU about 30 minutes.


## 7. Grading and the grader check

**[laptop]** Grade every result file. Number answers are graded by code; "why", FinanceBench and decline answers by the LLM grader (`eval/judge.py`, Haiku 5.5). Already graded answers are not graded again:

```bash
.venv/bin/python eval/run_experiments.py grade --set v2
```

```bash
.venv/bin/python eval/run_experiments.py grade --set financebench
```

**[laptop]** Grader check: regrade a random sample of 60 LLM grades with Sonnet 5.5. It prints the agreement (we got 54/60) and writes `eval/results/grader_check.csv`, whose empty `human` column is for a person:

```bash
.venv/bin/python eval/run_experiments.py agreement --model claude-sonnet-5-5
```

**[laptop]** A quick table per set:

```bash
.venv/bin/python eval/run_experiments.py summary --set v2
```


## 8. E2-E5: retrieval, latency, load and scaling

**E2, retrieval (no LLM).** [laptop, tunnel open] BM25 vs semantic vs hybrid on the correct filing, for both sets. It writes `eval/results/retrieval/v2.json` and `financebench.json`:

```bash
.venv/bin/python eval/retrieval_eval.py
```

**E3, latency per step.** Nothing extra to run: every system-A answer stores its step timings. The
report uses the Ollama run on the instance (step 6), where no network sits between the steps.

**E4, load test without the LLM.** [ec2] 1, 10 and 50 concurrent users for 30 s each, then name the file after the instance type:

```bash
docker compose run --rm -T api python eval/load_test.py --users 1 10 50 --seconds 30
```

```bash
mv eval/results/load_test.json eval/results/load_test_c7i.8xlarge.json
```

To compare instance types: stop the instance [aws], change its type (Actions → Instance settings →
Change instance type, e.g. t3.large), start it, run `docker compose up -d db`, and repeat these two
commands with the new name.

**E5, Spark scaling.** [ec2] The text job on 100 fixed filings and the facts job on all 20,609 files, on 1 to 32 cores (about 20 minutes; run it when nothing else uses the cores):

```bash
nohup bash cloud/scaling_experiment.sh > data/bulk/scaling.log 2>&1 &
```

```bash
cp data/bulk/scaling.csv eval/results/
```

**[laptop]** Fetch the E4 and E5 files from the instance:

```bash
rsync -av -e "ssh -i ~/.ssh/finqa.pem" "ubuntu@<host>:financial-agent-project/eval/results/load_test_*.json" "ubuntu@<host>:financial-agent-project/eval/results/scaling.csv" eval/results/
```


## 9. All tables in one file

**[laptop]** Write `eval/RESULTS_v2.md` from everything in `eval/results/`:

```bash
.venv/bin/python eval/summarize.py
```

This is the file the report and slides take their numbers from. The result files of our runs are
committed in `eval/results/`, so this command alone reproduces every table without spending
anything.

Expected headline numbers (our test set, 50 questions; FinanceBench: Haiku on 150, Qwen on 50):

| | Haiku 5.5 | Qwen2.5-7B (Mac) | Qwen2.5-3B (Mac) |
|---|---|---|---|
| A, our set | 96% | 84% | 62% |
| A0, our set | 96% | 84% | 62% |
| C, our set | 70% | 38% | 40% |
| A, FinanceBench | 69% | 12% | 16% |
| C, FinanceBench | 56% | 22% | 24% |

Qwen2.5-3B on the EC2 CPU (Ollama), system A: 54%. Retrieval, hybrid: hit@1 0.50 / MRR 0.61 on our
"why" questions (MD&A), hit@10 0.65 / MRR 0.40 on FinanceBench. Load test: 285 questions/s at 10
users. Scaling: text job 8.5x faster on 32 cores than on 1, facts job 7.0x.


## 10. Serve the app and shut down

**[ec2]** Start the API and the frontend, then open `http://<host>:8501`:

```bash
docker compose up -d
```

**[ec2]** Before stopping the instance, stop the containers so Postgres shuts down cleanly:

```bash
docker compose stop
```

**[aws]** Instance state → **Stop** (not Terminate). A stopped instance costs only its disk
(~$0.65/day); the database and the files stay. The public DNS name changes at the next start, so
update `<host>`, and the "My IP" rules if your own IP changed.

At the end of the project: **Terminate** the instance (this deletes the disk) and delete the S3
bucket if you made one.


## What can differ when you rerun

- **LLM answers are not fully deterministic.** Local models run with temperature 0. Claude runs
  with the API's default sampling, so a rerun can change a few "why" and FinanceBench answers by a
  point or two. The committed result files are the ones the reported numbers come from.
- **SEC data grows.** A later run of step 3 finds more filings, and the "latest quarter" questions
  (M01, M02) move to a newer quarter; rebuild the answer key (step 5) in that case.
- **Timings depend on the machine.** E3-E5 were measured on c7i.8xlarge in us-east-1 with nothing
  else running. Laptop runs through the SSH tunnel add network time to every database call.
- **The grader is an LLM.** Its agreement with a stronger model is 54/60 (90%); a person's check of
  `eval/results/grader_check.csv` is still open.


## Versions used

| Component | Version |
|---|---|
| Python | 3.12 |
| Postgres + pgvector | `pgvector/pgvector:pg16` image |
| Ollama | `ollama/ollama:0.6.2` image, `qwen2.5:3b-instruct` |
| PySpark | 4.3.0 (local mode, Java from Debian's `default-jre-headless`) |
| fastembed | 0.9.1, model `sentence-transformers/all-MiniLM-L6-v2` (384 dims) |
| anthropic | 2.9.0; models `claude-haiku-5-5` (system and grader), `claude-sonnet-5-5` (grader check) |
| mlx-lm | 0.32.2; `mlx-community/Qwen2.5-7B-Instruct-4bit`, `mlx-community/Qwen2.5-3B-Instruct-4bit` |
| psycopg / pgvector | 3.3.9 / 0.5.1 |
| pypdf | 7.1.1 |
| FastAPI / Streamlit | 0.163.1 / 1.67.0 |
| EC2 | c7i.8xlarge, Ubuntu 26.04 LTS, 250 GB gp3, us-east-1 |

`requirements.txt` gives minimum versions; the table above is what the reported runs used.
