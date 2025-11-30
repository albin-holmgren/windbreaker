# Windbreaker Architecture

## Overview

Windbreaker is a triangular arbitrage bot for Solana that detects and executes profitable trades across DEX routes using the Jupiter aggregator.

## System Design

```
┌─────────────────────────────────────────────────────────────┐
│                      Main Loop                               │
│  ┌─────────┐    ┌──────────┐    ┌──────────┐    ┌────────┐ │
│  │  Poll   │ →  │ Simulate │ →  │  Decide  │ →  │Execute │ │
│  └─────────┘    └──────────┘    └──────────┘    └────────┘ │
└─────────────────────────────────────────────────────────────┘
       ↓                ↓                              ↓
┌──────────────┐  ┌────────────┐               ┌─────────────┐
│ Jupiter API  │  │ Arb Engine │               │    RPC      │
└──────────────┘  └────────────┘               └─────────────┘
                                                      ↓
                                               ┌─────────────┐
                                               │   Solana    │
                                               └─────────────┘
```

## Components

### 1. Configuration (`config.py`)

Centralized configuration management:
- Loads environment variables
- Defines token addresses and decimals
- Sets trading parameters
- Manages network selection (devnet/mainnet)

Key structures:
- `Config` dataclass with all settings
- `TOKENS` / `TOKENS_DEVNET` token definitions
- `DEFAULT_TRIANGLES` candidate arbitrage paths

### 2. Wallet (`wallet.py`)

Secure wallet abstraction:
- Loads keypair from base58 private key
- Provides signing capabilities
- Supports both legacy and versioned transactions

Security considerations:
- Never logs or exposes private key
- Validates key format on load

### 3. RPC Client (`rpc.py`)

Robust RPC communication:
- Async HTTP requests to Solana RPC
- Token bucket rate limiting
- Exponential backoff on errors
- Transaction submission and confirmation

Key methods:
- `get_balance()` - Check SOL balance
- `send_transaction()` - Submit signed tx
- `confirm_transaction()` - Wait for confirmation
- `simulate_transaction()` - Pre-flight simulation

### 4. Arbitrage Engine (`arb_engine.py`)

Core arbitrage logic:
- Fetches quotes from Jupiter API
- Simulates triangular paths
- Calculates profit after fees/slippage
- Ranks opportunities by net profit

Key classes:
- `Quote` - Single swap quote data
- `TriangleOpportunity` - Complete arbitrage opportunity
- `ArbitrageEngine` - Main scanning and simulation

### 5. Executor (`executor.py`)

Transaction execution:
- Builds swap transactions via Jupiter
- Signs with wallet
- Submits to network
- Handles confirmation and errors

Execution modes:
- Atomic (single tx via Jupiter route)
- Sequential (fallback, 3 separate txs)

### 6. Monitor (`monitor.py`)

Observability and alerts:
- Telegram notifications
- CSV metrics logging
- Error tracking
- Statistics aggregation

## Data Flow

### Scan Cycle

```
1. Engine.scan_triangles()
   ├── For each path (A, B, C):
   │   ├── get_quote(A → B)
   │   ├── get_quote(B → C)
   │   ├── get_quote(C → A)
   │   └── Calculate net profit
   └── Return sorted opportunities

2. If opportunity.net_profit >= threshold:
   └── Executor.execute_triangle()
       ├── Get combined route quote
       ├── Build swap transaction
       ├── Sign transaction
       ├── Simulate (optional)
       ├── Send transaction
       └── Wait for confirmation

3. Monitor.on_trade_executed()
   ├── Log to CSV
   └── Send Telegram alert
```

### Quote Flow

```
Jupiter Quote API
    ↓
┌─────────────────────┐
│ Quote Response      │
│ - inAmount          │
│ - outAmount         │
│ - priceImpactPct    │
│ - routePlan         │
└─────────────────────┘
    ↓
Engine processes into TriangleOpportunity
    ↓
If profitable → Executor builds tx
```

## Triangular Arbitrage Logic

### Concept

Triangular arbitrage exploits price discrepancies across three trading pairs:

```
     A ───→ B
      ↖   ↙
        C
```

If `A → B → C → A` yields more A than started, profit exists.

### Profit Calculation

```python
# Gross profit
profit_amount = final_amount - input_amount
profit_pct = (profit_amount / input_amount) * 100

# Net profit (after fees)
tx_cost_in_token = estimate_tx_cost()
net_profit = profit_amount - tx_cost_in_token
net_profit_pct = (net_profit / input_amount) * 100
```

### Fee Components

1. **DEX fees**: ~0.25-0.30% per swap (varies by pool)
2. **Transaction fee**: ~0.000005 SOL base + priority fee
3. **Slippage**: Configured tolerance (default 0.5%)

Total cost for 3 swaps: ~1% + tx fees

## Rate Limiting

### Token Bucket Algorithm

```python
class RateLimiter:
    def __init__(self, max_per_second):
        self.tokens = max_per_second
        self.last_update = now()
    
    async def acquire(self):
        # Refill tokens based on elapsed time
        # Wait if no tokens available
```

### Backoff Strategy

On RPC errors:
```
retry_delay = min(base * 2^failures, max_delay)
```

Default: 1s base, 60s max

## Error Handling

### Transaction Failures

1. **Simulation fails**: Don't submit, log error
2. **Submission fails**: Log, don't retry immediately
3. **Confirmation timeout**: Log as uncertain, don't retry
4. **Partial execution**: (Sequential mode) Log warning, positions may be unbalanced

### RPC Errors

1. **Rate limited (429)**: Apply backoff
2. **Connection error**: Retry with backoff
3. **Timeout**: Retry with backoff

## Performance Considerations

### Latency

- RPC call: 50-200ms
- Jupiter quote: 100-300ms
- Total cycle: ~500-1000ms minimum

### Optimization Opportunities

1. **Premium RPC**: Lower latency endpoints
2. **Geyser streaming**: Real-time updates vs polling
3. **Rust port**: Faster execution
4. **MEV protection**: Jito bundles

## Security Model

### Key Management

- Private key loaded from env var
- Never persisted to disk
- Never logged or transmitted

### Transaction Safety

- Simulation before execution
- Slippage limits enforced
- No partial execution in atomic mode

### Operational Security

- Start with small amounts
- Monitor all executions
- Keep hot wallet minimal
