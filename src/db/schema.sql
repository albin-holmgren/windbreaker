-- Trade Telemetry Schema for Windbreaker Copy Trading Bot
-- Run this against your Postgres database to create all tables

-- ============================================================================
-- 1) TRADES TABLE - Core trade record (one row per position)
-- ============================================================================
CREATE TABLE IF NOT EXISTS trades (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id UUID NOT NULL,  -- Links Cupsey detection -> our execution -> position
    
    -- Token identification
    token_mint VARCHAR(64) NOT NULL,
    token_symbol VARCHAR(32),
    token_name VARCHAR(128),
    token_program VARCHAR(64),  -- SPL vs Token-2022
    token_decimals INT,
    
    -- Wallet info
    trader_wallet VARCHAR(64) NOT NULL,  -- Cupsey wallet
    bot_wallet VARCHAR(64) NOT NULL,     -- Our wallet
    
    -- Trade type and status
    trade_type VARCHAR(16) NOT NULL,  -- 'buy' or 'sell'
    status VARCHAR(32) NOT NULL DEFAULT 'pending',  -- pending, executed, failed, skipped
    
    -- Timestamps
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    executed_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    
    -- Cupsey's trade details
    their_signature VARCHAR(128),
    their_sol_amount DECIMAL(20, 9),
    their_token_amount DECIMAL(30, 0),
    their_dex VARCHAR(32),
    their_block_time TIMESTAMPTZ,
    their_slot BIGINT,
    
    -- Our trade details
    our_signature VARCHAR(128),
    our_sol_amount DECIMAL(20, 9),
    our_token_amount DECIMAL(30, 0),
    our_dex VARCHAR(32),
    our_block_time TIMESTAMPTZ,
    our_slot BIGINT,
    
    -- Position sizing
    position_size_sol DECIMAL(20, 9),
    copy_pct DECIMAL(5, 2),
    
    -- Entry decision (why we entered)
    entry_reason VARCHAR(64),  -- copied_buy, rebuy, etc.
    filters_passed JSONB,      -- All filter results at entry
    
    -- Exit decision (why we sold)
    exit_reason VARCHAR(64),   -- copied_sell, stop_loss, trailing_stop, take_profit, mcap_stop_loss, trader_exited, abandoned
    exit_pnl_pct DECIMAL(10, 4),
    exit_mcap_usd DECIMAL(20, 2),
    exit_time_in_trade_sec INT,
    cupsey_still_holding BOOLEAN,
    
    -- Final PnL
    realized_pnl_sol DECIMAL(20, 9),
    realized_pnl_usd DECIMAL(20, 4),
    realized_pnl_pct DECIMAL(10, 4),
    total_fees_sol DECIMAL(20, 9),
    
    -- Performance metrics
    max_profit_pct DECIMAL(10, 4),   -- MFE (Max Favorable Excursion)
    max_drawdown_pct DECIMAL(10, 4), -- MAE (Max Adverse Excursion)
    time_to_peak_sec INT,
    time_to_exit_sec INT,
    
    -- Skip/fail info (if not executed)
    skip_reason VARCHAR(256),
    error_message TEXT,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trades_token_mint ON trades(token_mint);
CREATE INDEX IF NOT EXISTS idx_trades_correlation_id ON trades(correlation_id);
CREATE INDEX IF NOT EXISTS idx_trades_detected_at ON trades(detected_at);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);

-- ============================================================================
-- 2) MARKET_SNAPSHOTS TABLE - Market data at specific moments
-- ============================================================================
CREATE TABLE IF NOT EXISTS market_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trade_id UUID REFERENCES trades(id) ON DELETE CASCADE,
    correlation_id UUID NOT NULL,
    token_mint VARCHAR(64) NOT NULL,
    
    -- Snapshot timing
    snapshot_type VARCHAR(32) NOT NULL,  -- 'detection', 'entry', 'exit', 'follow_up_1m', 'follow_up_5m', etc.
    snapshot_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    minutes_after_event INT,  -- For follow-up snapshots
    
    -- Data source
    data_source VARCHAR(32),  -- 'dexscreener', 'pumpfun', 'jupiter', 'none'
    data_missing_reason VARCHAR(128),
    
    -- Price data
    price_usd DECIMAL(30, 18),
    price_sol DECIMAL(30, 18),
    
    -- Market metrics
    market_cap_usd DECIMAL(20, 2),
    fully_diluted_valuation DECIMAL(20, 2),
    liquidity_usd DECIMAL(20, 2),
    
    -- Volume metrics
    volume_5m_usd DECIMAL(20, 2),
    volume_1h_usd DECIMAL(20, 2),
    volume_24h_usd DECIMAL(20, 2),
    
    -- Transaction counts
    txns_5m_buys INT,
    txns_5m_sells INT,
    txns_1h_buys INT,
    txns_1h_sells INT,
    txns_24h_buys INT,
    txns_24h_sells INT,
    
    -- Price changes
    price_change_m5_pct DECIMAL(10, 4),
    price_change_h1_pct DECIMAL(10, 4),
    price_change_h6_pct DECIMAL(10, 4),
    price_change_h24_pct DECIMAL(10, 4),
    
    -- Age
    token_age_minutes DECIMAL(15, 2),
    pair_age_minutes DECIMAL(15, 2),
    
    -- Pair info
    pair_address VARCHAR(64),
    pair_dex VARCHAR(32),
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_snapshots_trade_id ON market_snapshots(trade_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_token_mint ON market_snapshots(token_mint);
CREATE INDEX IF NOT EXISTS idx_snapshots_type ON market_snapshots(snapshot_type);
CREATE INDEX IF NOT EXISTS idx_snapshots_correlation_id ON market_snapshots(correlation_id);

-- ============================================================================
-- 3) EXECUTION_DETAILS TABLE - How the swap was executed
-- ============================================================================
CREATE TABLE IF NOT EXISTS execution_details (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trade_id UUID REFERENCES trades(id) ON DELETE CASCADE,
    correlation_id UUID NOT NULL,
    
    -- Execution identity
    executor VARCHAR(16) NOT NULL,  -- 'cupsey' or 'bot'
    execution_type VARCHAR(16) NOT NULL,  -- 'buy' or 'sell'
    
    -- Transaction details
    signature VARCHAR(128),
    slot BIGINT,
    block_time TIMESTAMPTZ,
    
    -- Programs involved
    program_ids JSONB,  -- Array of program IDs in the transaction
    dex_used VARCHAR(32),
    
    -- Pump.fun specific
    pumpfun_bonding_curve VARCHAR(64),
    pumpfun_coin_id VARCHAR(64),
    pumpfun_pool_type VARCHAR(32),  -- 'pump', 'pump-amm', 'raydium', etc.
    
    -- Raydium specific
    raydium_pool_id VARCHAR(64),
    raydium_amm_id VARCHAR(64),
    
    -- Jupiter specific
    jupiter_route JSONB,  -- Full route info
    jupiter_route_hops INT,
    jupiter_dexes_used JSONB,  -- Array of DEX names in route
    jupiter_quote_in DECIMAL(30, 0),
    jupiter_quote_out DECIMAL(30, 0),
    jupiter_price_impact_pct DECIMAL(10, 6),
    jupiter_route_score DECIMAL(10, 6),
    jupiter_no_route_reason VARCHAR(256),
    
    -- Requested amounts
    requested_in_amount DECIMAL(30, 0),
    requested_out_min DECIMAL(30, 0),
    slippage_bps_configured INT,
    
    -- Actual amounts
    actual_in_amount DECIMAL(30, 0),
    actual_out_amount DECIMAL(30, 0),
    effective_price DECIMAL(30, 18),
    
    -- Realized slippage
    realized_slippage_bps INT,
    price_impact_realized_pct DECIMAL(10, 6),
    
    -- Fees and costs
    priority_fee_lamports BIGINT,
    compute_units_used BIGINT,
    tx_fee_lamports BIGINT,
    total_cost_sol DECIMAL(20, 9),
    
    -- Timing
    submit_at TIMESTAMPTZ,
    confirm_at TIMESTAMPTZ,
    send_to_confirm_ms INT,
    
    -- Retries and errors
    attempt_number INT DEFAULT 1,
    total_retries INT DEFAULT 0,
    errors JSONB,  -- Array of error messages/codes
    final_status VARCHAR(32),  -- 'success', 'failed', 'timeout'
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_execution_trade_id ON execution_details(trade_id);
CREATE INDEX IF NOT EXISTS idx_execution_correlation_id ON execution_details(correlation_id);
CREATE INDEX IF NOT EXISTS idx_execution_signature ON execution_details(signature);

-- ============================================================================
-- 4) TOKEN_RISK_DATA TABLE - Token safety/risk metrics
-- ============================================================================
CREATE TABLE IF NOT EXISTS token_risk_data (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trade_id UUID REFERENCES trades(id) ON DELETE CASCADE,
    correlation_id UUID NOT NULL,
    token_mint VARCHAR(64) NOT NULL,
    
    -- Captured at
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Authority flags
    has_mint_authority BOOLEAN,
    has_freeze_authority BOOLEAN,
    mint_authority_address VARCHAR(64),
    freeze_authority_address VARCHAR(64),
    
    -- Token-2022 extensions
    is_token_2022 BOOLEAN,
    has_transfer_fee BOOLEAN,
    transfer_fee_bps INT,
    has_permanent_delegate BOOLEAN,
    permanent_delegate_address VARCHAR(64),
    has_non_transferable BOOLEAN,
    extensions JSONB,  -- Full list of extensions
    
    -- Holder distribution
    holders_count INT,
    top10_holders_pct DECIMAL(10, 4),
    top20_holders_pct DECIMAL(10, 4),
    dev_wallet_pct DECIMAL(10, 4),
    dev_wallet_address VARCHAR(64),
    
    -- LP/Liquidity info
    lp_locked_pct DECIMAL(10, 4),
    lp_burn_pct DECIMAL(10, 4),
    top_lp_holders JSONB,
    
    -- RugCheck data
    rugcheck_score INT,
    rugcheck_risk_level VARCHAR(32),
    rugcheck_flags JSONB,
    
    -- Creator info
    creator_wallet VARCHAR(64),
    is_trader_creator BOOLEAN,
    creator_other_tokens INT,
    creator_rug_history BOOLEAN,
    
    -- Social/metadata
    has_website BOOLEAN,
    has_twitter BOOLEAN,
    has_telegram BOOLEAN,
    metadata_uri VARCHAR(512),
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_risk_trade_id ON token_risk_data(trade_id);
CREATE INDEX IF NOT EXISTS idx_risk_token_mint ON token_risk_data(token_mint);

-- ============================================================================
-- 5) POST_TRADE_FOLLOWUPS TABLE - What happened after we sold
-- ============================================================================
CREATE TABLE IF NOT EXISTS post_trade_followups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trade_id UUID REFERENCES trades(id) ON DELETE CASCADE,
    correlation_id UUID NOT NULL,
    token_mint VARCHAR(64) NOT NULL,
    
    -- Reference point
    exit_price_usd DECIMAL(30, 18),
    exit_price_sol DECIMAL(30, 18),
    exit_at TIMESTAMPTZ NOT NULL,
    
    -- Follow-up timing
    followup_minutes INT NOT NULL,  -- 1, 3, 5, 10, 30, 60
    followup_at TIMESTAMPTZ NOT NULL,
    
    -- Price at follow-up
    price_usd DECIMAL(30, 18),
    price_sol DECIMAL(30, 18),
    
    -- Market data at follow-up
    market_cap_usd DECIMAL(20, 2),
    liquidity_usd DECIMAL(20, 2),
    volume_since_exit_usd DECIMAL(20, 2),
    
    -- Counterfactual analysis
    pnl_if_held_pct DECIMAL(10, 4),  -- What would PnL be if we held to this point
    pnl_if_held_sol DECIMAL(20, 9),
    pnl_if_held_usd DECIMAL(20, 4),
    
    -- Price movement
    price_change_since_exit_pct DECIMAL(10, 4),
    highest_price_since_exit DECIMAL(30, 18),
    lowest_price_since_exit DECIMAL(30, 18),
    best_exit_pnl_pct DECIMAL(10, 4),  -- Best possible exit since our sell
    worst_case_pnl_pct DECIMAL(10, 4), -- Worst case if held
    
    -- Analysis flags
    price_recovered BOOLEAN,  -- Did price go above our exit price?
    stop_loss_was_correct BOOLEAN,  -- Was stop loss the right call?
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_followup_trade_id ON post_trade_followups(trade_id);
CREATE INDEX IF NOT EXISTS idx_followup_token_mint ON post_trade_followups(token_mint);
CREATE INDEX IF NOT EXISTS idx_followup_minutes ON post_trade_followups(followup_minutes);

-- ============================================================================
-- 6) SKIPPED_TRADES TABLE - Trades we detected but didn't copy
-- ============================================================================
CREATE TABLE IF NOT EXISTS skipped_trades (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id UUID NOT NULL,
    
    -- Token info
    token_mint VARCHAR(64) NOT NULL,
    token_symbol VARCHAR(32),
    
    -- Trader info
    trader_wallet VARCHAR(64) NOT NULL,
    their_signature VARCHAR(128),
    their_sol_amount DECIMAL(20, 9),
    their_dex VARCHAR(32),
    
    -- Detection time
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Why skipped
    skip_reason VARCHAR(256) NOT NULL,
    skip_category VARCHAR(64),  -- 'filter_mcap', 'filter_liquidity', 'filter_age', 'concurrency', 'error', etc.
    
    -- Market data at skip time
    market_cap_usd DECIMAL(20, 2),
    liquidity_usd DECIMAL(20, 2),
    volume_24h_usd DECIMAL(20, 2),
    price_change_1h_pct DECIMAL(10, 4),
    txns_1h INT,
    token_age_minutes DECIMAL(15, 2),
    
    -- Filter thresholds (what we required)
    required_min_mcap DECIMAL(20, 2),
    required_min_liquidity DECIMAL(20, 2),
    required_min_volume DECIMAL(20, 2),
    required_min_age INT,
    required_max_pump_pct DECIMAL(10, 4),
    
    -- Holder data if available
    top10_holders_pct DECIMAL(10, 4),
    dev_holdings_pct DECIMAL(10, 4),
    
    -- Error details if applicable
    error_code VARCHAR(64),
    error_message TEXT,
    
    -- What happened after we skipped (for regret analysis)
    price_1h_later DECIMAL(30, 18),
    price_change_1h_later_pct DECIMAL(10, 4),
    would_have_profited BOOLEAN,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_skipped_token_mint ON skipped_trades(token_mint);
CREATE INDEX IF NOT EXISTS idx_skipped_reason ON skipped_trades(skip_reason);
CREATE INDEX IF NOT EXISTS idx_skipped_detected_at ON skipped_trades(detected_at);

-- ============================================================================
-- 7) FAILED_EXECUTIONS TABLE - Detailed error tracking
-- ============================================================================
CREATE TABLE IF NOT EXISTS failed_executions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trade_id UUID REFERENCES trades(id) ON DELETE SET NULL,
    correlation_id UUID NOT NULL,
    
    -- Token and trade info
    token_mint VARCHAR(64) NOT NULL,
    execution_type VARCHAR(16) NOT NULL,  -- 'buy' or 'sell'
    
    -- Attempt details
    attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempt_number INT NOT NULL,
    
    -- Method tried
    method VARCHAR(32),  -- 'pumpfun', 'jupiter', 'raydium'
    pool_type VARCHAR(32),
    
    -- Error details
    error_code VARCHAR(64),
    error_message TEXT,
    error_category VARCHAR(64),  -- 'no_route', 'slippage', 'insufficient_balance', 'program_error', 'timeout', etc.
    
    -- State at failure
    token_balance_at_attempt DECIMAL(30, 0),
    sol_balance_at_attempt DECIMAL(20, 9),
    liquidity_at_attempt DECIMAL(20, 2),
    
    -- What we tried
    requested_amount DECIMAL(30, 0),
    slippage_bps INT,
    priority_fee BIGINT,
    
    -- Response details
    rpc_response JSONB,
    simulation_error TEXT,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_failed_trade_id ON failed_executions(trade_id);
CREATE INDEX IF NOT EXISTS idx_failed_token_mint ON failed_executions(token_mint);
CREATE INDEX IF NOT EXISTS idx_failed_error_code ON failed_executions(error_code);

-- ============================================================================
-- 8) CUPSEY_TRADES TABLE - All detected Cupsey trades (for analysis)
-- ============================================================================
CREATE TABLE IF NOT EXISTS cupsey_trades (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id UUID NOT NULL,
    
    -- Trade details
    signature VARCHAR(128) NOT NULL UNIQUE,
    wallet VARCHAR(64) NOT NULL,
    trade_type VARCHAR(16) NOT NULL,  -- 'buy' or 'sell'
    
    -- Token info
    token_mint VARCHAR(64) NOT NULL,
    token_symbol VARCHAR(32),
    
    -- Amounts
    sol_amount DECIMAL(20, 9),
    token_amount DECIMAL(30, 0),
    
    -- Execution details
    dex VARCHAR(32),
    block_time TIMESTAMPTZ,
    slot BIGINT,
    
    -- Detection timing
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    detection_latency_ms INT,  -- Time from block to our detection
    
    -- Market data at detection
    market_cap_usd DECIMAL(20, 2),
    liquidity_usd DECIMAL(20, 2),
    price_usd DECIMAL(30, 18),
    
    -- Did we copy?
    copied BOOLEAN NOT NULL DEFAULT FALSE,
    copy_trade_id UUID REFERENCES trades(id) ON DELETE SET NULL,
    skip_reason VARCHAR(256),
    
    -- Cupsey's result (if we can track)
    cupsey_entry_price DECIMAL(30, 18),
    cupsey_exit_price DECIMAL(30, 18),
    cupsey_pnl_pct DECIMAL(10, 4),
    cupsey_hold_time_sec INT,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cupsey_signature ON cupsey_trades(signature);
CREATE INDEX IF NOT EXISTS idx_cupsey_token_mint ON cupsey_trades(token_mint);
CREATE INDEX IF NOT EXISTS idx_cupsey_detected_at ON cupsey_trades(detected_at);
CREATE INDEX IF NOT EXISTS idx_cupsey_copied ON cupsey_trades(copied);

-- ============================================================================
-- Update trigger for trades.updated_at
-- ============================================================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

DROP TRIGGER IF EXISTS update_trades_updated_at ON trades;
CREATE TRIGGER update_trades_updated_at
    BEFORE UPDATE ON trades
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();
