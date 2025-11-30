# RunPod Deployment Guide

This guide covers deploying Windbreaker to RunPod for 24/7 operation.

## Why RunPod?

- **Low latency**: Data centers close to Solana validators
- **Flexible compute**: Scale up/down as needed
- **Spot instances**: Cost-effective for testing
- **GPU optional**: Only needed if adding ML features

## Prerequisites

1. RunPod account with credits
2. SSH key pair for pod access
3. Configured `.env` file (not in repo)
4. Helius/QuickNode RPC endpoint

## Pod Selection

### For Testing (Devnet)

- **Template**: Ubuntu 22.04
- **Instance**: CPU Medium ($0.10/hr)
- **Storage**: 10GB minimum

### For Production

- **Template**: Ubuntu 22.04 or Secure Cloud
- **Instance**: A6000 Spot ($0.60/hr) or CPU Large
- **Storage**: 20GB recommended
- **Location**: Choose closest to your RPC endpoint

## Step-by-Step Deployment

### 1. Create RunPod Account

1. Go to [runpod.io](https://runpod.io)
2. Create account and add credits
3. Add your SSH public key in Settings → SSH Keys

### 2. Launch a Pod

1. Click "Deploy" → "GPU Cloud" (or "CPU Cloud")
2. Select template: Ubuntu 22.04
3. Choose instance type
4. Enable SSH in advanced options
5. Click "Deploy"

### 3. Connect to Pod

Wait for pod to start, then copy SSH command:

```bash
ssh root@pod-xyz.runpod.net -p 12345 -i ~/.ssh/your_key
```

### 4. Manual Installation

If not using the automated script:

```bash
# Update system
apt-get update && apt-get upgrade -y

# Install dependencies
apt-get install -y git python3 python3-pip python3-venv

# Install PM2
apt-get install -y nodejs npm
npm install -g pm2

# Clone repository
git clone https://github.com/yourusername/windbreaker.git
cd windbreaker

# Set up Python environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure environment
nano .env  # Paste your configuration
chmod 600 .env

# Run tests
python -m pytest tests/test_simulation.py -v

# Start with PM2
pm2 start src/main.py --name windbreaker --interpreter .venv/bin/python
pm2 save
```

### 5. Automated Deployment

Use the provided script from your local machine:

```bash
# Make executable
chmod +x scripts/runpod-deploy.sh

# Deploy (replace with your pod address)
./scripts/runpod-deploy.sh root@pod-xyz.runpod.net ~/.env.windbreaker
```

The script will:
1. Install system dependencies
2. Clone/update the repository
3. Copy your environment file
4. Set up Python virtual environment
5. Run tests
6. Start the bot with PM2

## Managing the Bot

### View Logs

```bash
pm2 logs windbreaker
pm2 logs windbreaker --lines 100
```

### Monitor

```bash
pm2 monit
```

### Restart

```bash
pm2 restart windbreaker
```

### Stop

```bash
pm2 stop windbreaker
```

### Check Status

```bash
pm2 list
pm2 describe windbreaker
```

## Persistent Operation

### PM2 Startup

Ensure PM2 restarts on pod reboot:

```bash
pm2 startup
pm2 save
```

### Log Rotation

Configure PM2 log rotation:

```bash
pm2 install pm2-logrotate
pm2 set pm2-logrotate:max_size 10M
pm2 set pm2-logrotate:retain 7
```

## Cost Optimization

### Spot Instances

Use spot instances for testing:
- ~60% cheaper than on-demand
- May be interrupted (rare)
- Good for devnet testing

### Scaling Down

- Reduce poll interval during low-activity periods
- Use CPU instance if no ML features

## Monitoring from Outside

### Telegram Alerts

Configure in `.env`:
```
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

Get bot token from [@BotFather](https://t.me/botfather)

### Health Checks

Add to crontab for external monitoring:

```bash
crontab -e
```

Add:
```
*/5 * * * * curl -s https://your-health-check-service.com/ping/windbreaker
```

## Troubleshooting

### Bot Not Starting

1. Check logs: `pm2 logs windbreaker --lines 200`
2. Verify `.env` exists and has correct permissions
3. Test configuration: `python -c "from src.config import load_config; load_config()"`

### RPC Errors

1. Check RPC endpoint is accessible
2. Verify API key is valid
3. Check rate limits in Helius/QuickNode dashboard

### Low Balance Warnings

1. Fund wallet with more SOL
2. Reduce trade amount if needed

### Pod Disconnects

1. Enable persistent connection: `pm2 startup`
2. Use `screen` or `tmux` for manual sessions

## Security Checklist

- [ ] `.env` file has permission 600
- [ ] Private key is valid base58
- [ ] Using HTTPS RPC endpoint
- [ ] Telegram alerts configured
- [ ] Hot wallet has minimal funds
- [ ] PM2 is saving state

## Updating the Bot

```bash
cd /root/windbreaker
git pull origin main
source .venv/bin/activate
pip install -r requirements.txt
pm2 restart windbreaker
```

Or use the deploy script again from your local machine.
