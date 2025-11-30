# Windbreaker — Solana Triangular Arbitrage Bot — Build & Run

**Purpose:**
This document is a developer-ready specification and deployment guide for a simple, safe, no-flash-loan Solana triangular-arbitrage bot intended to be hosted on RunPod. It is written so you can hand it to a developer ("Cursor") to implement, test on devnet, and deploy to a production RunPod instance.

---

## 1 — High-level overview

* **Goal:** scan Solana DEX routes (Jupiter/Raydium/Orca/etc.), detect triangular price mismatches (A → B → C → A) and execute atomic multi-swap transactions when the simulated net profit (after estimated fees + slippage) exceeds a configurable threshold (default `0.5%`).
* **Constraints:** no flash loans; use only wallet-funded trades. Start with small capital (e.g. $100) for testing.
* **Host:** RunPod (GPU instance) for low-latency polling & optional ML inference. Repo runs CLI service (Python initially) and can be extended to Rust for speed.

---

## 2 — Deliverables for Cursor

1. A GitHub repository with the structure below.
2. A working Python implementation (initial MVP) that: scans, simulates, and executes profitable triangular loops on devnet and mainnet (after config swap).
3. Clear configuration via environment variables and a sample `.env.example` (no real secrets in repo).
4. RunPod deployment instructions & a `runpod-deploy.sh` script that automates pod creation steps and starts the bot.
5. PM2 process config for 24/7 run and a basic system of logs & alerts (Telegram webhook alerts on executed trades / errors).
6. Unit tests + an integration test script that runs on devnet (simulates X loops and verifies expected behavior).
7. README for devs and a short `OPERATION.md` for non-dev operator instructions.

---

## 3 — Recommended repo structure

```
windbreaker/
├─ README.md
├─ LICENSE
├─ .gitignore
├─ .env.example
├─ docs/
│  ├─ ARCHITECTURE.md
│  └─ RUNPOD.md
├─ src/
│  ├─ main.py                # Entrypoint (Python MVP)
│  ├─ arb_engine.py          # Core: find and simulate triangles
│  ├─ executor.py            # Build and send transaction(s) (Jupiter SDK usage)
│  ├─ wallet.py              # Wallet abstraction (load key from env/secret)
│  ├─ rpc.py                 # RPC client wrapper (Helius/QuickNode support)
│  ├─ monitor.py             # Alerts / health checks / Telegram
│  └─ config.py              # Config loader (env vars)
├─ scripts/
│  ├─ runpod-deploy.sh
│  └─ start-prod.sh
├─ tests/
│  ├─ test_simulation.py
│  └─ test_devnet_integration.sh
├─ infra/
│  └─ runpod-template.json   # optional: pre-fill RunPod settings
└─ ci/
   └─ github-actions.yaml
```

---

## 4 — Tech stack & third-party services

* **Language:** Python 3.10+ for MVP. (Optional: port to Rust/Anchor for speed later.)
* **Solana libs:** `solana-py` (or alternative `solders`), Jupiter Quote API for route simulation, `solders`/`solana` for tx building.
* **RPC provider:** Helius (recommended) or QuickNode. Use paid/pro plan for low-latency mainnet.
* **Hosting:** RunPod (spot for cost savings while testing). Use GPU instance only if you plan ML inference; otherwise CPU instance fine.
* **Process manager:** PM2 or systemd.
* **Monitoring/alerts:** Telegram (bot token + chat id), optional Sentry for exceptions.

---

## 5 — Configuration & secrets

Store secrets only in environment variables in RunPod / secret manager. Provide `.env.example` with placeholders.

**Important env vars (example)**

```
# Network
RPC_URL=https://rpc.helius.xyz/?api-key=YOUR_HELIUS_KEY  # devnet/mainnet switchable
NETWORK=devnet

# Wallet
WALLET_PRIVATE_KEY_BASE58=    # Exported Phantom private key (base58) — **DO NOT** commit
WALLET_ADDRESS=

# Trading
MIN_PROFIT_PCT=0.5            # 0.5% = minimum net profit threshold
TRADE_AMOUNT_USD=10          # starting trade size in USD equivalent
SLIPPAGE_BPS=50              # 0.5% default
POLL_INTERVAL_MS=500        # 500ms polling (tune for RPC limits)

# Jupiter API
JUPITER_QUOTE_API=https://quote-api.jup.ag/v6/quote

# Alerts
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Ops
LOG_LEVEL=INFO
```

**Security**

* Never commit private keys. Use RunPod env secrets or mount a file with restricted perms.
* For production balances, recommend Ledger hardware wallet + remote signing flow or HSM.

---

## 6 — MVP behavior (what the bot must do)

1. **Poll**: Periodically pull price quotes from Jupiter for selected token sets and candidate triangular paths (configurable list). Start with SOL ↔ USDC ↔ ETH.
2. **Simulate**: For a chosen input amount, compute the expected output after the 3-step route, incorporate Jupiter `slippageBps` and add an estimated fee model (DEX fees + ~0.01 SOL tx cost). Compute `net_profit_pct`.
3. **Decide**: If `net_profit_pct >= MIN_PROFIT_PCT`, build a single atomic swap transaction (use Jupiter route to build signed multi-swap transaction) and send it.
4. **Execute**: Send the transaction signed by the wallet. Wait for confirmation; if confirmed, log result & send Telegram alert with trade details (timestamp, coins, input, output, net profit% and tx signature).
5. **Fail-safe**: If tx fails or reverts, detect and log; never attempt partial recovery that could leave an imbalanced position.
6. **Rate limit**: Backoff if RPC returns rate-limited or errors.

Acceptance tests: the bot must simulate correctly on devnet and successfully execute a 3-swap devnet transaction (no real money) with proper confirmation logs.

---

## 7 — Dev & deployment steps (step-by-step)

### Local/dev workflow

1. Clone repo.
2. Copy `.env.example` → `.env` and fill devnet RPC and a devnet wallet. (Create Phantom devnet wallet or use CLI keypair.)
3. `python -m venv .venv && source .venv/bin/activate`
4. `pip install -r requirements.txt` (requirements include `solana`, `requests`, `python-dotenv`, `pm2` optional)
5. Run tests: `pytest tests/`
6. Start devnet bot: `python src/main.py` (should run in devnet mode and only use test wallet funds)

### RunPod deployment (basic)

1. Create RunPod account and add credits.
2. Start a pod: choose Ubuntu 22.04 (or Secure Cloud GPU template if you want GPUs). Recommended instance: A6000 (spot for testing) or CPU medium for cheap runs.
3. SSH into pod. Install git, python, pip.
4. `git clone <repo>`
5. Create `.env` via RunPod secret editor or `echo` into file with secure perms (`chmod 600`).
6. Start bot under PM2: `pm2 start src/main.py --name windbreaker --interpreter python3`
7. Use `pm2 logs windbreaker` and `pm2 monit` to monitor.

### RunPod automation script (`scripts/runpod-deploy.sh`)

Include a simple script that SSHs to the pod, pulls latest, installs deps, copies `.env`, and restarts PM2. Cursor should implement SSH keys + non-interactive deploy.

---

## 8 — Testing & validation

**Devnet integration test:** `tests/test_devnet_integration.sh` should:

* Start the bot in devnet mode for 5 minutes.
* Force-simulate a triangular route using pre-seeded pools (devnet) and confirm that the bot logs the correct simulated profit.
* Attempt a harmless 3-swap with tiny amounts and confirm on-chain completion.

**Unit tests:** `tests/test_simulation.py` must validate the `simulate_triangle()` math for multiple slippage/fee scenarios.

**Manual validation steps before mainnet:**

* Run on devnet for 24–72 hours without enabling mainnet setting.
* Ensure no secret is committed.
* Check health logs & monitor any failed txs.

---

## 9 — Observability & ops

* **Logging:** Structured JSON logs to stdout (timestamp, level, event, details). Use PM2 to capture and rotate logs.
* **Alerts:** Telegram on each executed trade. Critical errors (RPC down, repeated tx failures) send high-priority alerts.
* **Metrics:** Simple counters (successful_trades, failed_trades, profit_usd) stored locally in a CSV or to a lightweight metrics endpoint (optional Prometheus pushgateway).

---

## 10 — Security & compliance checklist

* Never store private keys in Git. Use RunPod environment secrets or file mounts with `600` perm.
* Audit all third-party open-source code before merging.
* Use small balances during testing and keep hot wallet funds minimal.
* If going to production with material funds, integrate hardware signing (Ledger) and multi-sig.
* Maintain an incident playbook for drained wallet or compromised secrets.

---

## 11 — Optional performance & production upgrades (phase 2)

* **Rust port** of core engine (Anchor + `solders`) for faster sims & lower latency.
* **Premium RPC** (Helius Pro / QuickNode) with Geyser stream to reduce poll latency.
* **Jito/MEV bundling** integration to increase chance of being included early in blocks (requires careful tip management).
* **Colocation / proximity**: move pods to a data center near Solana validators (if budget allows) for microseconds gains.
* **Auto-switching** across multiple chains/dexes and dynamic pair lists.

---

## 12 — Acceptance criteria for Cursor

1. Repo created with above structure, README, and `.env.example`.
2. `src/main.py` implements the MVP loop: poll → simulate → execute on devnet.
3. Tests pass: `pytest tests/` returns green locally.
4. RunPod `runpod-deploy.sh` starts the service and the bot runs for 24 hours on devnet without secret leaks.
5. Deliver documentation files: `ARCHITECTURE.md`, `RUNPOD.md`, and `OPERATION.md`.
6. Short demo video (optional) showing a successful devnet 3-swap execution and Telegram alert.

---

## 13 — Time & cost estimate (rough)

* MVP Python implementation: 1–3 days (1 dev).
* Tests + RunPod scripts + docs: +1 day.
* Rust port & high-performance infra: +1–2 weeks (if desired).

RunPod costs (approx): $0.20–$0.60/hr for spot A6000 (testing) → $6–$18/day. Helius Pro RPC: ~$49/mo for stable low-latency endpoint.

---

## 14 — Next steps for you

* Share this document with Cursor and confirm dev environment access (GitHub, RunPod).
* Provide a small devnet fund and RunPod credit.
* Ask Cursor to run devnet tests and send back the repo link + instruction to run locally.

---

If you want, I can also export a ready-to-use `README.md` and a complete `src/main.py` starter script for Cursor to begin from — say the word and I will add those files to this repo doc set.
