#!/bin/bash
#
# RunPod Deployment Script for Windbreaker
# 
# This script automates deployment to a RunPod instance.
# 
# Prerequisites:
# - SSH access to RunPod pod configured
# - SSH key added to RunPod
# - .env file ready (not in repo)
#
# Usage:
#   ./runpod-deploy.sh <pod-ssh-address> [env-file-path]
#
# Example:
#   ./runpod-deploy.sh user@pod-xyz.runpod.net ~/.env.windbreaker
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
REPO_URL="${REPO_URL:-https://github.com/yourusername/windbreaker.git}"
REMOTE_DIR="/root/windbreaker"
PM2_APP_NAME="windbreaker"

# Parse arguments
POD_SSH="$1"
ENV_FILE="${2:-.env}"

if [ -z "$POD_SSH" ]; then
    echo -e "${RED}ERROR: Pod SSH address required${NC}"
    echo "Usage: $0 <pod-ssh-address> [env-file-path]"
    echo "Example: $0 user@pod-xyz.runpod.net ~/.env.windbreaker"
    exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
    echo -e "${RED}ERROR: Environment file not found: $ENV_FILE${NC}"
    exit 1
fi

echo -e "${GREEN}=========================================${NC}"
echo -e "${GREEN}Windbreaker RunPod Deployment${NC}"
echo -e "${GREEN}=========================================${NC}"
echo ""
echo "Target: $POD_SSH"
echo "Remote dir: $REMOTE_DIR"
echo ""

# Function to run command on remote
remote_exec() {
    ssh -o StrictHostKeyChecking=no "$POD_SSH" "$1"
}

# Function to copy file to remote
remote_copy() {
    scp -o StrictHostKeyChecking=no "$1" "$POD_SSH:$2"
}

# Step 1: Check SSH connection
echo -e "${YELLOW}[1/7] Testing SSH connection...${NC}"
if ! remote_exec "echo 'SSH OK'"; then
    echo -e "${RED}ERROR: Cannot connect to pod via SSH${NC}"
    exit 1
fi
echo -e "${GREEN}✓ SSH connection successful${NC}"

# Step 2: Install dependencies on pod
echo ""
echo -e "${YELLOW}[2/7] Installing system dependencies...${NC}"
remote_exec "apt-get update -qq && apt-get install -y -qq git python3 python3-pip python3-venv nodejs npm > /dev/null 2>&1"
remote_exec "npm install -g pm2 > /dev/null 2>&1 || true"
echo -e "${GREEN}✓ System dependencies installed${NC}"

# Step 3: Clone or update repository
echo ""
echo -e "${YELLOW}[3/7] Setting up repository...${NC}"
remote_exec "
if [ -d '$REMOTE_DIR' ]; then
    cd '$REMOTE_DIR' && git fetch origin && git reset --hard origin/main
else
    git clone '$REPO_URL' '$REMOTE_DIR'
fi
"
echo -e "${GREEN}✓ Repository ready${NC}"

# Step 4: Copy environment file
echo ""
echo -e "${YELLOW}[4/7] Copying environment configuration...${NC}"
remote_copy "$ENV_FILE" "$REMOTE_DIR/.env"
remote_exec "chmod 600 '$REMOTE_DIR/.env'"
echo -e "${GREEN}✓ Environment file deployed${NC}"

# Step 5: Set up Python virtual environment
echo ""
echo -e "${YELLOW}[5/7] Setting up Python environment...${NC}"
remote_exec "
cd '$REMOTE_DIR'
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip > /dev/null
pip install -r requirements.txt > /dev/null
"
echo -e "${GREEN}✓ Python environment ready${NC}"

# Step 6: Run tests
echo ""
echo -e "${YELLOW}[6/7] Running tests...${NC}"
remote_exec "
cd '$REMOTE_DIR'
source .venv/bin/activate
python -m pytest tests/test_simulation.py -v --tb=short
"
echo -e "${GREEN}✓ Tests passed${NC}"

# Step 7: Start/restart PM2 process
echo ""
echo -e "${YELLOW}[7/7] Starting bot with PM2...${NC}"
remote_exec "
cd '$REMOTE_DIR'
pm2 delete '$PM2_APP_NAME' 2>/dev/null || true
pm2 start src/main.py --name '$PM2_APP_NAME' --interpreter '$REMOTE_DIR/.venv/bin/python' --cwd '$REMOTE_DIR'
pm2 save
"
echo -e "${GREEN}✓ Bot started${NC}"

# Show status
echo ""
echo -e "${GREEN}=========================================${NC}"
echo -e "${GREEN}Deployment Complete!${NC}"
echo -e "${GREEN}=========================================${NC}"
echo ""
echo "The bot is now running on the pod."
echo ""
echo "Useful commands (run on the pod):"
echo "  pm2 logs $PM2_APP_NAME     - View logs"
echo "  pm2 monit                  - Monitor all processes"
echo "  pm2 restart $PM2_APP_NAME  - Restart the bot"
echo "  pm2 stop $PM2_APP_NAME     - Stop the bot"
echo ""

# Get current status
echo "Current status:"
remote_exec "pm2 list"
