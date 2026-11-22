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
- Name `finqa`, image **Amazon Linux 2023** (64-bit x86)
- Type **c7i.4xlarge** (16 vCPU, 32 GB, about $0.71/h). With a 32-vCPU quota, **c7i.8xlarge** halves the embedding time for the same total cost.
- Key pair: create one named `finqa` (RSA, .pem) and save it as `~/.ssh/finqa.pem`, then `chmod 400 ~/.ssh/finqa.pem`
- Network: allow **SSH (22)** from *My IP*; add a rule for **TCP 8501** (Streamlit) from *My IP*
- Storage: **250 GB gp3**
- Advanced details → IAM instance profile: **finqa-ec2**

Connect:

```bash
ssh -i ~/.ssh/finqa.pem ec2-user@<public-ip>
```


## 3. Code and Docker

The repository is private, so copy your checkout from your laptop (this also works for
uncommitted changes). From the folder above the project:

```bash
rsync -av --exclude .venv --exclude .git --exclude data/bulk --exclude .env -e "ssh -i ~/.ssh/finqa.pem" financial-agent-project ec2-user@<public-ip>:~/
```

On the instance:

```bash
cd financial-agent-project && bash cloud/ec2_setup.sh
```

Log out and in again. Create `.env` from `.env.example` (API keys, and `SEC_USER_AGENT` with your
name and email), then build the image:

```bash
docker compose build
```


## 4. Download the SEC bulk files (about 15 minutes)

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

When it is done, keep a copy in S3:

```bash
bash cloud/backup_to_s3.sh finqa-yourname-2026
```


## 6. Serve the app

```bash
docker compose up -d
```

Open `http://<public-ip>:8501`. To save money afterwards: stop the instance, change the instance
type to **t3.large** (2 vCPU, 8 GB, about $0.08/h; Actions → Instance settings → Change instance
type), start it again and run `docker compose up -d`. The public IP changes after a stop.


## Scaling experiment (plan 16.8)

Same machine, different core counts, on one batch of filings:

```bash
docker compose run --rm api python spark/text_job.py --input data/bulk/text/raw --output /tmp/scale --cores 4 --max-batches 1
```

Repeat with `--cores 1`, `2`, `4`, `8`, `16` (use a new `--output` each time) and note the
passages per second it prints.


## Costs and teardown

| Item | Price (us-east-1) | Typical use |
|---|---|---|
| c7i.4xlarge | ~$0.71/h, only while running | ~1 day for the pipeline: ~$15-20 |
| t3.large afterwards | ~$0.08/h | app and experiments |
| EBS 250 GB gp3 | ~$20/month, also while stopped | delete at the end |
| S3, ~50-100 GB | ~$1-2/month | |

At the end of the project: terminate the instance (deletes its disk) and empty and delete the
bucket (`aws s3 rb s3://finqa-yourname-2026 --force`).
