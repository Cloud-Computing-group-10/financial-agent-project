#!/usr/bin/env bash
# Install Docker, Docker Compose and the AWS CLI on a fresh EC2 instance (Ubuntu or Amazon Linux 2023).
#   bash cloud/ec2_setup.sh      (then log out and in again, so your user can run docker)
set -euo pipefail

if command -v apt-get >/dev/null; then        # Ubuntu
  sudo apt-get update -q
  sudo apt-get install -y -q docker.io docker-compose-v2 unzip
  command -v aws >/dev/null || sudo snap install aws-cli --classic
else                                          # Amazon Linux 2023 (the AWS CLI is preinstalled)
  sudo dnf install -y docker git unzip
  sudo mkdir -p /usr/local/lib/docker/cli-plugins
  sudo curl -fsSL "https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-$(uname -m)" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
  sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
fi

sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
echo "Docker installed. Log out and in again, then run: docker compose version"
# Note: files written by the containers (data/bulk/parquet, text/...) belong to root; delete them with
#   docker run --rm -v $PWD/data:/app/data finqa rm -rf /app/data/bulk/<folder>
