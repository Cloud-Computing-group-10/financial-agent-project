# Running on AWS (Learner Lab)

What runs where:

| Part | AWS service | Why |
|---|---|---|
| Raw SEC files (pilot + bulk) | **S3** | Source of truth; Spark reads from here |
| Spark job over every SEC filer | **EMR** | The bulk XBRL file is several GB of JSON; Spark processes the companies in parallel |
| Postgres + API + Streamlit | **EC2** (one instance, Docker Compose) | The live query path; same `docker-compose.yml` as on a laptop |

Learner Lab rules that matter here:
- A session lasts about 4 hours. When it ends, EC2 instances are **stopped** (not deleted) and EMR clusters are **terminated**. Start the lab again before a demo; the app comes back when the instance starts.
- The AWS keys change every session. Copy them again from **AWS Details → AWS CLI** into `~/.aws/credentials`.
- You can't create IAM roles. Use the existing ones: `LabInstanceProfile` for EC2, `EMR_DefaultRole` and `EMR_EC2_DefaultRole` for EMR.
- Use region `us-east-1`.
- Anything left allocated (EBS disks, Elastic IPs, S3 data) keeps using credits. See "Costs and teardown".

Commands with `aws` run on your laptop (install the AWS CLI with `brew install awscli`) or in AWS CloudShell. The rest runs on the EC2 instance.


## 1. S3 bucket

Pick a globally unique name, e.g. `finqa-<yourname>-2026`:

```bash
aws s3 mb s3://finqa-yourname-2026 --region us-east-1
```


## 2. EC2 instance

In the EC2 console, **Launch instance**:
- Name `finqa`, image **Amazon Linux 2023**, type **t3.medium** (2 vCPU, 4 GB)
- Key pair: **vockey** (download `labsuser.pem` from AWS Details)
- Network: allow **SSH (22)** from your IP, and add a rule for **TCP 8501** (Streamlit) from your IP. Add **8080** too if you want to call the API directly.
- Storage: **40 GB** gp3 (the unzipped bulk file needs room before it goes to S3)
- Advanced details → IAM instance profile: **LabInstanceProfile** (gives the instance S3 access)

Connect:

```bash
chmod 400 labsuser.pem
```

```bash
ssh -i labsuser.pem ec2-user@<public-ip>
```

The repo is private, so copy your checkout from your laptop instead of cloning it. This also works for uncommitted changes. Run on your laptop, from the folder above the project:

```bash
rsync -av --exclude .venv --exclude .git --exclude data/bulk --exclude .env -e "ssh -i labsuser.pem" financial-agent-project ec2-user@<public-ip>:~/
```

Run the same command again to push later code changes. Then, on the instance, install Docker:

```bash
cd financial-agent-project && bash cloud/ec2_setup.sh
```

Log out and in again, then create `.env` (API keys, `SEC_USER_AGENT`) from `.env.example`.


## 3. Start the service and load the pilot

```bash
docker compose up -d --build
```

```bash
docker compose run --rm api python -m finqa.ingest
```

Open `http://<public-ip>:8501`. Keep a copy of the raw pilot files in S3:

```bash
aws s3 sync data/raw s3://finqa-yourname-2026/raw/
```


## 4. Bulk data to S3

On the instance (about 1.4 GB download, a few minutes):

```bash
bash cloud/bulk_to_s3.sh finqa-yourname-2026 "Your Name your.email@connect.ust.hk"
```


## 5. Spark on EMR

Create a cluster in the EMR console (**Create cluster**):
- Release **emr-7.x**, application bundle **Spark**
- Instance groups: 1 primary + 2 core, **m5.xlarge** (change the core count for the scaling experiment: 1, 2, 4)
- Service role **EMR_DefaultRole**, EC2 instance profile **EMR_EC2_DefaultRole**
- Cluster logs: `s3://finqa-yourname-2026/emr-logs/`
- Termination: **automatically terminate after 1 hour idle**, so a forgotten cluster doesn't burn credits

When the cluster is "Waiting", submit the job (from your laptop or the EC2 instance; the cluster id starts with `j-`):

```bash
bash cloud/emr_submit.sh finqa-yourname-2026 j-XXXXXXXXXXXX
```

The step's log (EMR console → Steps → stdout) prints the coverage table, the number of restatement conflicts, the row counts and the total time. Note the time and the worker count for the scaling experiment (plan 16.8). **Terminate the cluster** when you are done.


## 6. Load the Spark output into Postgres

On the EC2 instance:

```bash
aws s3 sync s3://finqa-yourname-2026/parquet data/bulk/parquet
```

```bash
docker compose run --rm api python -m finqa.load_bulk data/bulk/parquet
```

Now the frontend answers numeric questions about every filer, e.g. "What was Apple's revenue in Q2 2024?". "Why" questions still work only for the pilot companies, whose text is indexed.


## Costs and teardown

Rough prices in us-east-1:

| Item | Price | Typical use |
|---|---|---|
| EC2 t3.medium | ~$0.04/h, only while running | a few $ total |
| EBS 40 GB | ~$3.20/month, also while stopped | delete at the end |
| S3, a few GB | < $0.10/month | |
| EMR, 3 × m5.xlarge | ~$0.60/h incl. EMR fee | ~$1-3 per experiment |

At the end of the project: terminate the EC2 instance (deletes its disk), delete the bucket (`aws s3 rb s3://finqa-yourname-2026 --force`), and check that no EMR cluster is running.
