# Running on AWS: every SEC filer on one EC2 instance

What runs where:

| Part | Where | Why |
|---|---|---|
| SEC bulk files, downloaded 10-Qs, Spark output | instance disk (`data/bulk`), copied to **S3** | S3 is the long-term copy of the data lake |
| Spark jobs (numbers, then split + embed of 10-Q text) | **EC2**, Spark in local mode on all cores | One large machine is simpler than a cluster and as fast for this job size; the SEC download speed limit is the same either way |
| Postgres + API + Streamlit | the same **EC2** instance (Docker Compose) | Same `docker-compose.yml` as on a laptop |

Plan: launch a large instance for the pipeline (about a day), then stop it and change it to a small
type for the app and the experiments. The disk, the database and the data stay.

Region: `us-east-1`. Commands with `aws` run on the instance (the AWS CLI is preinstalled).


## 0. Account checks (once)

- **Plan:** an account on the AWS *Free plan* can only start small instance types. Upgrade to the
  *Paid plan* (Billing → Plans) to use the instance below; remaining credits stay on the account.
- **vCPU quota:** Service Quotas → Amazon EC2 → "Running On-Demand Standard (A, C, D, H, I, M, R, T, Z)
  instances" must be at least 16 (32 for the 8xlarge). New accounts often have less; request an
  increase there (can take a few hours).
- **Budget alert:** Billing → Budgets → create a monthly cost budget of $80 with an email alert.


## 1. S3 bucket and instance role (for the S3 copy)

- S3 → Create bucket, e.g. `finqa-<yourname>-2026`, region us-east-1, defaults otherwise.
- IAM → Roles → Create role → trusted entity **EC2** → policy **AmazonS3FullAccess** → name `finqa-ec2`.


## 2. Launch the instance

EC2 → Launch instance:
- Name `finqa`, image **Ubuntu** or **Amazon Linux 2023** (64-bit x86); `cloud/ec2_setup.sh` handles both
- Type **c7i.8xlarge** (32 vCPU, 64 GB, about $1.43/h; needs a 32-vCPU quota), or **c7i.4xlarge** (16 vCPU, about $0.71/h, twice the embedding time for the same cost)
- Key pair: create one named `finqa` (RSA, .pem) and save it as `~/.ssh/finqa.pem`, then `chmod 400 ~/.ssh/finqa.pem`
- Network: allow **SSH (22)** from *My IP*; add a rule for **TCP 8501** (Streamlit) from *My IP*
- Storage: **250 GB gp3**
- Advanced details → IAM instance profile: **finqa-ec2**

Connect:

```bash
ssh -i ~/.ssh/finqa.pem ubuntu@<public-ip>
```

(The user name is `ubuntu` on Ubuntu, `ec2-user` on Amazon Linux.)


## 3. Code and Docker

The repository is private, so copy your checkout from your laptop (this also works for
uncommitted changes). From the folder above the project:

```bash
rsync -av --exclude .venv --exclude .git --exclude data/bulk --exclude data/financebench/pdfs --exclude .env -e "ssh -i ~/.ssh/finqa.pem" financial-agent-project ubuntu@<public-ip>:~/
```

On the instance:

```bash
cd financial-agent-project && bash cloud/ec2_setup.sh
```

Log out and in again. Create `.env` from `.env.example`: `SEC_USER_AGENT` with your name and email
(needed for the downloads), and the API keys (`ANTHROPIC_API_KEY`, `HKUST_API_KEY`; needed for the
app and the experiments). Then build the image:

```bash
docker compose build
```


## 4. Download the SEC bulk files (a few minutes)

```bash
bash cloud/fetch_bulk.sh
```


## 5. Build the database for every filer

```bash
nohup bash cloud/run_pipeline.sh > data/bulk/pipeline.log 2>&1 &
```

The script runs the Spark facts job, loads the numbers, writes the list of 10-Qs, then downloads
them while Spark splits and embeds every finished batch and loads it into Postgres. Watch it with
`tail -f data/bulk/pipeline.log` and `tail -f data/bulk/download.log`. It survives logging out. If
it stops (or you stop it), start it again: finished work is skipped.

Options: `CORES=8` (Spark cores, default all), `FIRST_YEAR=2021` (fewer years, less download).

Measured on c7i.8xlarge (32 vCPU): the Spark facts job takes 40 s for 20,609 company files
(9,695 companies with 10-Qs since 2019, 140,436 filings, 3.0 million figures), loading them takes
1 minute. The download runs at SEC's limit, about 4 minutes per batch of 2,000 filings (about 5
hours in all). Embedding is the slow step: MD&A only (README 5.3) takes about 7 minutes per batch,
about 9 hours in all. Spark processes finished batches while the download continues.

When it is done, keep a copy in S3:

```bash
bash cloud/backup_to_s3.sh finqa-yourname-2026
```


## 6. FinanceBench filings

The FinanceBench PDFs (README 5.4) are parsed and embedded on a laptop (faster per core than the
server) and written into the cloud database through an SSH tunnel. Open the tunnel:

```bash
ssh -i ~/.ssh/finqa.pem -f -N -L 5433:localhost:5432 ubuntu@<public-ip>
```

Then, on the laptop, with the PDFs in `data/financebench/pdfs`:

```bash
DATABASE_URL=postgresql://finqa:finqa@localhost:5433/finqa .venv/bin/python -m finqa.pdf_docs
```

The same tunnel lets you query the cloud database from the laptop, e.g. to run the experiments
(README 8.3) with `DATABASE_URL` set this way. Postgres is only reachable through SSH; port 5432 is
not open to the internet.


## 7. Serve the app

```bash
docker compose up -d
```

Open `http://<public-ip>:8501`. To save money afterwards: stop the instance, change the instance
type to **t3.large** (2 vCPU, 8 GB, about $0.08/h; Actions → Instance settings → Change instance
type), start it again and run `docker compose up -d`. The public IP changes after a stop.


## Scaling experiment (plan 16.8)

Same machine, different core counts, on the same sample of filings. A full batch takes hours on
one core, so copy 200 filings of a finished batch into a sample batch first:

```bash
docker compose run --rm -T api sh -c 'mkdir -p data/bulk/scale/raw/batch=0000 && ls data/bulk/text/raw/batch=0005/*.htm.gz | head -200 | xargs -I{} cp {} data/bulk/scale/raw/batch=0000/ && touch data/bulk/scale/raw/batch=0000/_DONE'
```

```bash
docker compose run --rm -T api python spark/text_job.py --input data/bulk/scale/raw --output /tmp/scale --manifest data/bulk/text/manifest.csv --cores 4
```

Repeat with `--cores 1`, `2`, `4`, `8`, `16`, `32` and note the passages per second it prints
(each run starts from scratch, since `/tmp` is inside the container). Run it after the pipeline,
when nothing else uses the cores, and before changing the instance to a small type.


## Costs and teardown

| Item | Price (us-east-1) | Typical use |
|---|---|---|
| c7i.8xlarge | ~$1.43/h, only while running | ~10 h for the pipeline: ~$15 |
| t3.large afterwards | ~$0.08/h | app and experiments |
| EBS 250 GB gp3 | ~$20/month, also while stopped | delete at the end |
| S3, ~50-100 GB | ~$1-2/month | |

At the end of the project: terminate the instance (deletes its disk) and empty and delete the
bucket (`aws s3 rb s3://finqa-yourname-2026 --force`).
