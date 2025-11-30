#!/bin/bash
#
# Production Start Script for Windbreaker
# 
# This script starts the bot with PM2 for production use.
# It should be run on the production server.
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PM2_APP_NAME="windbreaker"

cd "$PROJECT_ROOT"

echo "Windbreaker Production Start"
echo "============================"
echo ""

# Check for .env
if [ ! -f ".env" ]; then
    echo "ERROR: .env file not found!"
    exit 1
fi

# Check network setting
NETWORK=$(grep "^NETWORK=" .env | cut -d'=' -f2)
echo "Network: $NETWORK"
echo ""

if [ "$NETWORK" == "devnet" ]; then
    echo "WARNING: Running on devnet. Set NETWORK=mainnet-beta for production."
    echo ""
fi

# Activate virtual environment
if [ -d ".venv" ]; then
    source .venv/bin/activate
else
    echo "Creating virtual environment..."
    python3 -m venv .venv
    source .venv/bin/activate
    pip install --upgrade pip
    pip install -r requirements.txt
fi

# Check if PM2 is installed
if ! command -v pm2 &> /dev/null; then
    echo "PM2 not found. Installing..."
    npm install -g pm2
fi

# Stop existing process if running
pm2 delete "$PM2_APP_NAME" 2>/dev/null || true

# Start with PM2
echo "Starting $PM2_APP_NAME..."
pm2 start src/main.py \
    --name "$PM2_APP_NAME" \
    --interpreter "$PROJECT_ROOT/.venv/bin/python" \
    --cwd "$PROJECT_ROOT" \
    --log "$PROJECT_ROOT/logs/windbreaker.log" \
    --time

# Save PM2 configuration
pm2 save

echo ""
echo "Bot started successfully!"
echo ""
echo "Commands:"
echo "  pm2 logs $PM2_APP_NAME     - View logs"
echo "  pm2 monit                  - Monitor processes"
echo "  pm2 restart $PM2_APP_NAME  - Restart bot"
echo "  pm2 stop $PM2_APP_NAME     - Stop bot"
echo ""

# Show status
pm2 list
