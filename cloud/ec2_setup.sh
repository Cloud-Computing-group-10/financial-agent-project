#!/usr/bin/env bash
# Install Docker and Docker Compose on a fresh Amazon Linux 2023 EC2 instance.
#   bash cloud/ec2_setup.sh      (then log out and in again, so your user can run docker)
set -euo pipefail

sudo dnf install -y docker git unzip
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

# Docker Compose v2 plugin
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -fsSL "https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-$(uname -m)" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

echo "Docker installed. Log out and in again, then run: docker compose version"
