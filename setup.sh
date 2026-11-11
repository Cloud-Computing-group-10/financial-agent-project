#!/usr/bin/env bash

set -e # Exit immediately if a command exits with a non-zero status

ENV_NAME="financial-agent-env"

echo "=== Starting Financial Agent Project Setup ==="

# 1. Environment creation using Conda (with fallback to venv)
if command -v conda &> /dev/null; then
    echo "--> Conda detected. Setting up Conda environment '${ENV_NAME}'..."
    # Initialize conda for shell script execution
    eval "$(conda shell.bash hook)"
    
    if conda info --envs | grep -q "^${ENV_NAME} "; then
        echo "Environment '${ENV_NAME}' already exists. Activating..."
    else
        echo "Creating new Conda environment with Python 3.11..."
        conda create -n "${ENV_NAME}" python=3.11 -y
    fi
    conda activate "${ENV_NAME}"
else
    echo "--> Conda not found. Falling back to standard Python venv..."
    if [ ! -d "venv" ]; then
        python3 -m venv venv
    fi
    source venv/bin/activate
fi

# 2. Upgrade pip and install required dependencies
echo "--> Installing dependencies..."
pip install --upgrade pip
pip install openai python-dotenv

# 3. Create .env template if it doesn't exist
if [ ! -f ".env" ]; then
    echo "--> Creating local .env template..."
    cat <<EOT > .env
HKUST_API_KEY=your_actual_api_key_here
EOT
    echo "Created .env file. Please update it with your actual HKUST API key."
fi

# 4. Create .env.example for repository reference
if [ ! -f ".env.example" ]; then
    cat <<EOT > .env.example
HKUST_API_KEY=your_api_key_here
EOT
fi

echo "=== Setup Complete! ==="
echo "To activate your environment in future terminal sessions, run:"
echo "  conda activate ${ENV_NAME}"