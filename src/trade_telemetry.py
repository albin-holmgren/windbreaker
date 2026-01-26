"""
Trade Telemetry Module - Comprehensive tracking of all trade data.

Captures:
- Cupsey trades (detected)
- Our trades (executed)
- Market snapshots (entry, exit, follow-ups)
- Execution details (slippage, fees, routes)
- Token risk data (holders, authorities)
- Post-trade follow-ups (counterfactual analysis)
- Skipped/failed trades
"""

import asyncio
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set
import json

import asyncpg
import aiohttp
import structlog

logger = structlog.get_logger()


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class MarketSnapshot:
    """Market data at a specific moment."""
    snapshot_type: str  # 'detection', 'entry', 'exit', 'follow_up_1m', etc.
    token_mint: str
    snapshot_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    # Data source
    data_source: Optional[str] = None
    data_missing_reason: Optional[str] = None
    
    # Price
    price_usd: Optional[Decimal] = None
    price_sol: Optional[Decimal] = None
    
    # Market metrics
    market_cap_usd: Optional[Decimal] = None
    fully_diluted_valuation: Optional[Decimal] = None
    liquidity_usd: Optional[Decimal] = None
    
    # Volume
    volume_5m_usd: Optional[Decimal] = None
    volume_1h_usd: Optional[Decimal] = None
    volume_24h_usd: Optional[Decimal] = None
    
    # Transactions
    txns_5m_buys: Optional[int] = None
    txns_5m_sells: Optional[int] = None
    txns_1h_buys: Optional[int] = None
    txns_1h_sells: Optional[int] = None
    txns_24h_buys: Optional[int] = None
    txns_24h_sells: Optional[int] = None
    
    # Price changes
    price_change_m5_pct: Optional[Decimal] = None
    price_change_h1_pct: Optional[Decimal] = None
    price_change_h6_pct: Optional[Decimal] = None
    price_change_h24_pct: Optional[Decimal] = None
    
    # Age
    token_age_minutes: Optional[Decimal] = None
    pair_age_minutes: Optional[Decimal] = None
    
    # Pair info
    pair_address: Optional[str] = None
    pair_dex: Optional[str] = None
    
    # For follow-ups
    minutes_after_event: Optional[int] = None


@dataclass
class ExecutionDetails:
    """How a swap was executed."""
    executor: str  # 'cupsey' or 'bot'
    execution_type: str  # 'buy' or 'sell'
    
    # Transaction
    signature: Optional[str] = None
    slot: Optional[int] = None
    block_time: Optional[datetime] = None
    
    # Programs
    program_ids: Optional[List[str]] = None
    dex_used: Optional[str] = None
    
    # Pump.fun specific
    pumpfun_bonding_curve: Optional[str] = None
    pumpfun_coin_id: Optional[str] = None
    pumpfun_pool_type: Optional[str] = None
    
    # Raydium specific
    raydium_pool_id: Optional[str] = None
    raydium_amm_id: Optional[str] = None
    
    # Jupiter specific
    jupiter_route: Optional[Dict] = None
    jupiter_route_hops: Optional[int] = None
    jupiter_dexes_used: Optional[List[str]] = None
    jupiter_quote_in: Optional[Decimal] = None
    jupiter_quote_out: Optional[Decimal] = None
    jupiter_price_impact_pct: Optional[Decimal] = None
    jupiter_route_score: Optional[Decimal] = None
    jupiter_no_route_reason: Optional[str] = None
    
    # Requested amounts
    requested_in_amount: Optional[Decimal] = None
    requested_out_min: Optional[Decimal] = None
    slippage_bps_configured: Optional[int] = None
    
    # Actual amounts
    actual_in_amount: Optional[Decimal] = None
    actual_out_amount: Optional[Decimal] = None
    effective_price: Optional[Decimal] = None
    
    # Realized slippage
    realized_slippage_bps: Optional[int] = None
    price_impact_realized_pct: Optional[Decimal] = None
    
    # Fees
    priority_fee_lamports: Optional[int] = None
    compute_units_used: Optional[int] = None
    tx_fee_lamports: Optional[int] = None
    total_cost_sol: Optional[Decimal] = None
    
    # Timing
    submit_at: Optional[datetime] = None
    confirm_at: Optional[datetime] = None
    send_to_confirm_ms: Optional[int] = None
    
    # Retries
    attempt_number: int = 1
    total_retries: int = 0
    errors: Optional[List[str]] = None
    final_status: Optional[str] = None


@dataclass
class TokenRiskData:
    """Token safety and risk metrics."""
    token_mint: str
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    # Authority flags
    has_mint_authority: Optional[bool] = None
    has_freeze_authority: Optional[bool] = None
    mint_authority_address: Optional[str] = None
    freeze_authority_address: Optional[str] = None
    
    # Token-2022
    is_token_2022: Optional[bool] = None
    has_transfer_fee: Optional[bool] = None
    transfer_fee_bps: Optional[int] = None
    has_permanent_delegate: Optional[bool] = None
    permanent_delegate_address: Optional[str] = None
    has_non_transferable: Optional[bool] = None
    extensions: Optional[List[str]] = None
    
    # Holder distribution
    holders_count: Optional[int] = None
    top10_holders_pct: Optional[Decimal] = None
    top20_holders_pct: Optional[Decimal] = None
    dev_wallet_pct: Optional[Decimal] = None
    dev_wallet_address: Optional[str] = None
    
    # LP info
    lp_locked_pct: Optional[Decimal] = None
    lp_burn_pct: Optional[Decimal] = None
    top_lp_holders: Optional[List[Dict]] = None
    
    # RugCheck
    rugcheck_score: Optional[int] = None
    rugcheck_risk_level: Optional[str] = None
    rugcheck_flags: Optional[List[str]] = None
    
    # Creator
    creator_wallet: Optional[str] = None
    is_trader_creator: Optional[bool] = None
    creator_other_tokens: Optional[int] = None
    creator_rug_history: Optional[bool] = None
    
    # Social
    has_website: Optional[bool] = None
    has_twitter: Optional[bool] = None
    has_telegram: Optional[bool] = None
    metadata_uri: Optional[str] = None


@dataclass
class TradeRecord:
    """Complete trade record."""
    correlation_id: str
    token_mint: str
    trader_wallet: str
    bot_wallet: str
    trade_type: str
    
    # Token info
    token_symbol: Optional[str] = None
    token_name: Optional[str] = None
    token_program: Optional[str] = None
    token_decimals: Optional[int] = None
    
    # Status
    status: str = "pending"
    
    # Timestamps
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    executed_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    
    # Cupsey's trade
    their_signature: Optional[str] = None
    their_sol_amount: Optional[Decimal] = None
    their_token_amount: Optional[Decimal] = None
    their_dex: Optional[str] = None
    their_block_time: Optional[datetime] = None
    their_slot: Optional[int] = None
    
    # Our trade
    our_signature: Optional[str] = None
    our_sol_amount: Optional[Decimal] = None
    our_token_amount: Optional[Decimal] = None
    our_dex: Optional[str] = None
    our_block_time: Optional[datetime] = None
    our_slot: Optional[int] = None
    
    # Position sizing
    position_size_sol: Optional[Decimal] = None
    copy_pct: Optional[Decimal] = None
    
    # Entry decision
    entry_reason: Optional[str] = None
    filters_passed: Optional[Dict] = None
    
    # Exit decision
    exit_reason: Optional[str] = None
    exit_pnl_pct: Optional[Decimal] = None
    exit_mcap_usd: Optional[Decimal] = None
    exit_time_in_trade_sec: Optional[int] = None
    cupsey_still_holding: Optional[bool] = None
    
    # PnL
    realized_pnl_sol: Optional[Decimal] = None
    realized_pnl_usd: Optional[Decimal] = None
    realized_pnl_pct: Optional[Decimal] = None
    total_fees_sol: Optional[Decimal] = None
    
    # Performance
    max_profit_pct: Optional[Decimal] = None
    max_drawdown_pct: Optional[Decimal] = None
    time_to_peak_sec: Optional[int] = None
    time_to_exit_sec: Optional[int] = None
    
    # Skip/fail
    skip_reason: Optional[str] = None
    error_message: Optional[str] = None
    
    # Attached data
    market_snapshots: List[MarketSnapshot] = field(default_factory=list)
    execution_details: List[ExecutionDetails] = field(default_factory=list)
    token_risk: Optional[TokenRiskData] = None


# ============================================================================
# Telemetry Manager
# ============================================================================

class TradeTelemetry:
    """Manages all trade telemetry capture and storage."""
    
    def __init__(self, database_url: Optional[str] = None):
        self.database_url = database_url or os.getenv("DATABASE_URL")
        self.pool: Optional[asyncpg.Pool] = None
        self.session: Optional[aiohttp.ClientSession] = None
        
        # Pending follow-ups to schedule
        self._follow_up_tasks: Dict[str, asyncio.Task] = {}
        
        # In-memory buffer for fast writes (flush periodically)
        self._buffer: List[Dict] = []
        self._buffer_lock = asyncio.Lock()
        
    async def start(self):
        """Initialize database connection and HTTP session."""
        if self.database_url:
            try:
                self.pool = await asyncpg.create_pool(
                    self.database_url,
                    min_size=2,
                    max_size=10,
                    command_timeout=30
                )
                logger.info("telemetry_db_connected")
                
                # Run schema migration
                await self._ensure_schema()
            except Exception as e:
                logger.error("telemetry_db_connection_failed", error=str(e))
                self.pool = None
        
        self.session = aiohttp.ClientSession()
        
    async def stop(self):
        """Cleanup resources."""
        # Cancel pending follow-up tasks
        for task in self._follow_up_tasks.values():
            task.cancel()
        
        if self.session:
            await self.session.close()
        
        if self.pool:
            await self.pool.close()
            
    async def _ensure_schema(self):
        """Ensure database schema exists."""
        if not self.pool:
            return
            
        try:
            # Read schema file
            schema_path = os.path.join(os.path.dirname(__file__), "db", "schema.sql")
            if os.path.exists(schema_path):
                with open(schema_path, "r") as f:
                    schema_sql = f.read()
                
                async with self.pool.acquire() as conn:
                    await conn.execute(schema_sql)
                    
                logger.info("telemetry_schema_applied")
        except Exception as e:
            logger.error("telemetry_schema_error", error=str(e))

    # ========================================================================
    # Market Data Fetching
    # ========================================================================
    
    async def fetch_market_snapshot(
        self,
        token_mint: str,
        snapshot_type: str,
        minutes_after_event: Optional[int] = None
    ) -> MarketSnapshot:
        """Fetch comprehensive market data for a token."""
        snapshot = MarketSnapshot(
            snapshot_type=snapshot_type,
            token_mint=token_mint,
            minutes_after_event=minutes_after_event
        )
        
        # Try DexScreener first
        try:
            async with self.session.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs", [])
                    
                    if pairs:
                        snapshot.data_source = "dexscreener"
                        
                        # Get best pair (highest liquidity)
                        best_pair = max(pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0) or 0)
                        
                        # Price
                        snapshot.price_usd = Decimal(str(best_pair.get("priceUsd", 0) or 0))
                        snapshot.price_sol = Decimal(str(best_pair.get("priceNative", 0) or 0))
                        
                        # Market cap
                        snapshot.market_cap_usd = Decimal(str(best_pair.get("marketCap", 0) or best_pair.get("fdv", 0) or 0))
                        snapshot.fully_diluted_valuation = Decimal(str(best_pair.get("fdv", 0) or 0))
                        
                        # Aggregate liquidity and volume across all pairs
                        total_liq = sum(p.get("liquidity", {}).get("usd", 0) or 0 for p in pairs)
                        snapshot.liquidity_usd = Decimal(str(total_liq))
                        
                        # Volume
                        vol_5m = sum(p.get("volume", {}).get("m5", 0) or 0 for p in pairs)
                        vol_1h = sum(p.get("volume", {}).get("h1", 0) or 0 for p in pairs)
                        vol_24h = sum(p.get("volume", {}).get("h24", 0) or 0 for p in pairs)
                        snapshot.volume_5m_usd = Decimal(str(vol_5m))
                        snapshot.volume_1h_usd = Decimal(str(vol_1h))
                        snapshot.volume_24h_usd = Decimal(str(vol_24h))
                        
                        # Transactions
                        txns = best_pair.get("txns", {})
                        snapshot.txns_5m_buys = txns.get("m5", {}).get("buys", 0)
                        snapshot.txns_5m_sells = txns.get("m5", {}).get("sells", 0)
                        snapshot.txns_1h_buys = txns.get("h1", {}).get("buys", 0)
                        snapshot.txns_1h_sells = txns.get("h1", {}).get("sells", 0)
                        snapshot.txns_24h_buys = txns.get("h24", {}).get("buys", 0)
                        snapshot.txns_24h_sells = txns.get("h24", {}).get("sells", 0)
                        
                        # Price changes
                        price_change = best_pair.get("priceChange", {})
                        snapshot.price_change_m5_pct = Decimal(str(price_change.get("m5", 0) or 0))
                        snapshot.price_change_h1_pct = Decimal(str(price_change.get("h1", 0) or 0))
                        snapshot.price_change_h6_pct = Decimal(str(price_change.get("h6", 0) or 0))
                        snapshot.price_change_h24_pct = Decimal(str(price_change.get("h24", 0) or 0))
                        
                        # Pair age
                        created_at = best_pair.get("pairCreatedAt")
                        if created_at:
                            import time
                            age_ms = time.time() * 1000 - created_at
                            snapshot.pair_age_minutes = Decimal(str(age_ms / 60000))
                        
                        snapshot.pair_address = best_pair.get("pairAddress")
                        snapshot.pair_dex = best_pair.get("dexId")
                    else:
                        snapshot.data_missing_reason = "no_pairs_found"
        except Exception as e:
            snapshot.data_missing_reason = f"dexscreener_error: {str(e)}"
        
        # If DexScreener failed, try Pump.fun for pump tokens
        if not snapshot.data_source and "pump" in token_mint.lower():
            try:
                async with self.session.get(
                    f"https://frontend-api.pump.fun/coins/{token_mint}",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        snapshot.data_source = "pumpfun"
                        snapshot.market_cap_usd = Decimal(str(data.get("usd_market_cap", 0) or 0))
                        
                        # Token age from created_timestamp
                        created_ts = data.get("created_timestamp")
                        if created_ts:
                            import time
                            age_ms = time.time() * 1000 - created_ts
                            snapshot.token_age_minutes = Decimal(str(age_ms / 60000))
            except Exception as e:
                if not snapshot.data_missing_reason:
                    snapshot.data_missing_reason = f"pumpfun_error: {str(e)}"
        
        return snapshot

    async def fetch_token_risk_data(self, token_mint: str) -> TokenRiskData:
        """Fetch token risk and safety metrics."""
        risk = TokenRiskData(token_mint=token_mint)
        
        # Try RugCheck API
        try:
            async with self.session.get(
                f"https://api.rugcheck.xyz/v1/tokens/{token_mint}/report",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    # Risk score
                    risk.rugcheck_score = data.get("score")
                    risk.rugcheck_risk_level = data.get("riskLevel")
                    risk.rugcheck_flags = data.get("risks", [])
                    
                    # Token info
                    token_info = data.get("token", {})
                    risk.has_mint_authority = token_info.get("mintAuthority") is not None
                    risk.has_freeze_authority = token_info.get("freezeAuthority") is not None
                    risk.mint_authority_address = token_info.get("mintAuthority")
                    risk.freeze_authority_address = token_info.get("freezeAuthority")
                    
                    # Holder distribution
                    holders = data.get("topHolders", [])
                    if holders:
                        risk.holders_count = data.get("holderCount", len(holders))
                        top10_total = sum(h.get("pct", 0) for h in holders[:10])
                        top20_total = sum(h.get("pct", 0) for h in holders[:20])
                        risk.top10_holders_pct = Decimal(str(top10_total))
                        risk.top20_holders_pct = Decimal(str(top20_total))
                    
                    # Creator
                    risk.creator_wallet = data.get("creator")
                    
                    # LP info
                    lp_info = data.get("liquidityProviders", {})
                    risk.lp_locked_pct = Decimal(str(lp_info.get("lockedPct", 0) or 0))
                    risk.lp_burn_pct = Decimal(str(lp_info.get("burnedPct", 0) or 0))
                    
        except Exception as e:
            logger.debug("rugcheck_fetch_error", token=token_mint[:8], error=str(e))
        
        return risk

    # ========================================================================
    # Trade Recording
    # ========================================================================
    
    async def record_cupsey_trade(
        self,
        signature: str,
        wallet: str,
        trade_type: str,
        token_mint: str,
        sol_amount: Decimal,
        token_amount: Optional[Decimal],
        dex: str,
        block_time: Optional[datetime],
        slot: Optional[int],
        market_snapshot: Optional[MarketSnapshot] = None,
        copied: bool = False,
        skip_reason: Optional[str] = None
    ) -> str:
        """Record a detected Cupsey trade."""
        correlation_id = str(uuid.uuid4())
        
        if not self.pool:
            logger.debug("telemetry_no_db", event="cupsey_trade_not_recorded")
            return correlation_id
        
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO cupsey_trades (
                        correlation_id, signature, wallet, trade_type, token_mint,
                        sol_amount, token_amount, dex, block_time, slot,
                        detected_at, market_cap_usd, liquidity_usd, price_usd,
                        copied, skip_reason
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
                    ON CONFLICT (signature) DO NOTHING
                """,
                    correlation_id, signature, wallet, trade_type, token_mint,
                    sol_amount, token_amount, dex, block_time, slot,
                    datetime.now(timezone.utc),
                    market_snapshot.market_cap_usd if market_snapshot else None,
                    market_snapshot.liquidity_usd if market_snapshot else None,
                    market_snapshot.price_usd if market_snapshot else None,
                    copied, skip_reason
                )
                
            logger.debug("cupsey_trade_recorded", 
                        correlation_id=correlation_id[:8],
                        token=token_mint[:8],
                        type=trade_type)
                        
        except Exception as e:
            logger.error("cupsey_trade_record_error", error=str(e))
        
        return correlation_id

    async def record_trade(self, trade: TradeRecord) -> Optional[str]:
        """Record a complete trade with all attached data."""
        if not self.pool:
            logger.debug("telemetry_no_db", event="trade_not_recorded")
            return None
        
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    # Insert main trade record
                    trade_id = await conn.fetchval("""
                        INSERT INTO trades (
                            correlation_id, token_mint, token_symbol, token_name,
                            token_program, token_decimals, trader_wallet, bot_wallet,
                            trade_type, status, detected_at, executed_at, closed_at,
                            their_signature, their_sol_amount, their_token_amount,
                            their_dex, their_block_time, their_slot,
                            our_signature, our_sol_amount, our_token_amount,
                            our_dex, our_block_time, our_slot,
                            position_size_sol, copy_pct, entry_reason, filters_passed,
                            exit_reason, exit_pnl_pct, exit_mcap_usd, exit_time_in_trade_sec,
                            cupsey_still_holding, realized_pnl_sol, realized_pnl_usd,
                            realized_pnl_pct, total_fees_sol, max_profit_pct,
                            max_drawdown_pct, time_to_peak_sec, time_to_exit_sec,
                            skip_reason, error_message
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                            $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25,
                            $26, $27, $28, $29, $30, $31, $32, $33, $34, $35, $36, $37,
                            $38, $39, $40, $41, $42, $43, $44
                        ) RETURNING id
                    """,
                        trade.correlation_id, trade.token_mint, trade.token_symbol,
                        trade.token_name, trade.token_program, trade.token_decimals,
                        trade.trader_wallet, trade.bot_wallet, trade.trade_type,
                        trade.status, trade.detected_at, trade.executed_at, trade.closed_at,
                        trade.their_signature, trade.their_sol_amount, trade.their_token_amount,
                        trade.their_dex, trade.their_block_time, trade.their_slot,
                        trade.our_signature, trade.our_sol_amount, trade.our_token_amount,
                        trade.our_dex, trade.our_block_time, trade.our_slot,
                        trade.position_size_sol, trade.copy_pct, trade.entry_reason,
                        json.dumps(trade.filters_passed) if trade.filters_passed else None,
                        trade.exit_reason, trade.exit_pnl_pct, trade.exit_mcap_usd,
                        trade.exit_time_in_trade_sec, trade.cupsey_still_holding,
                        trade.realized_pnl_sol, trade.realized_pnl_usd, trade.realized_pnl_pct,
                        trade.total_fees_sol, trade.max_profit_pct, trade.max_drawdown_pct,
                        trade.time_to_peak_sec, trade.time_to_exit_sec,
                        trade.skip_reason, trade.error_message
                    )
                    
                    # Insert market snapshots
                    for snapshot in trade.market_snapshots:
                        await conn.execute("""
                            INSERT INTO market_snapshots (
                                trade_id, correlation_id, token_mint, snapshot_type,
                                snapshot_at, minutes_after_event, data_source,
                                data_missing_reason, price_usd, price_sol,
                                market_cap_usd, fully_diluted_valuation, liquidity_usd,
                                volume_5m_usd, volume_1h_usd, volume_24h_usd,
                                txns_5m_buys, txns_5m_sells, txns_1h_buys, txns_1h_sells,
                                txns_24h_buys, txns_24h_sells, price_change_m5_pct,
                                price_change_h1_pct, price_change_h6_pct, price_change_h24_pct,
                                token_age_minutes, pair_age_minutes, pair_address, pair_dex
                            ) VALUES (
                                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                                $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24,
                                $25, $26, $27, $28, $29, $30
                            )
                        """,
                            trade_id, trade.correlation_id, snapshot.token_mint,
                            snapshot.snapshot_type, snapshot.snapshot_at,
                            snapshot.minutes_after_event, snapshot.data_source,
                            snapshot.data_missing_reason, snapshot.price_usd,
                            snapshot.price_sol, snapshot.market_cap_usd,
                            snapshot.fully_diluted_valuation, snapshot.liquidity_usd,
                            snapshot.volume_5m_usd, snapshot.volume_1h_usd,
                            snapshot.volume_24h_usd, snapshot.txns_5m_buys,
                            snapshot.txns_5m_sells, snapshot.txns_1h_buys,
                            snapshot.txns_1h_sells, snapshot.txns_24h_buys,
                            snapshot.txns_24h_sells, snapshot.price_change_m5_pct,
                            snapshot.price_change_h1_pct, snapshot.price_change_h6_pct,
                            snapshot.price_change_h24_pct, snapshot.token_age_minutes,
                            snapshot.pair_age_minutes, snapshot.pair_address, snapshot.pair_dex
                        )
                    
                    # Insert execution details
                    for exec_detail in trade.execution_details:
                        await conn.execute("""
                            INSERT INTO execution_details (
                                trade_id, correlation_id, executor, execution_type,
                                signature, slot, block_time, program_ids, dex_used,
                                pumpfun_bonding_curve, pumpfun_coin_id, pumpfun_pool_type,
                                raydium_pool_id, raydium_amm_id, jupiter_route,
                                jupiter_route_hops, jupiter_dexes_used, jupiter_quote_in,
                                jupiter_quote_out, jupiter_price_impact_pct,
                                jupiter_route_score, jupiter_no_route_reason,
                                requested_in_amount, requested_out_min, slippage_bps_configured,
                                actual_in_amount, actual_out_amount, effective_price,
                                realized_slippage_bps, price_impact_realized_pct,
                                priority_fee_lamports, compute_units_used, tx_fee_lamports,
                                total_cost_sol, submit_at, confirm_at, send_to_confirm_ms,
                                attempt_number, total_retries, errors, final_status
                            ) VALUES (
                                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                                $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24,
                                $25, $26, $27, $28, $29, $30, $31, $32, $33, $34, $35,
                                $36, $37, $38, $39, $40, $41
                            )
                        """,
                            trade_id, trade.correlation_id, exec_detail.executor,
                            exec_detail.execution_type, exec_detail.signature,
                            exec_detail.slot, exec_detail.block_time,
                            json.dumps(exec_detail.program_ids) if exec_detail.program_ids else None,
                            exec_detail.dex_used, exec_detail.pumpfun_bonding_curve,
                            exec_detail.pumpfun_coin_id, exec_detail.pumpfun_pool_type,
                            exec_detail.raydium_pool_id, exec_detail.raydium_amm_id,
                            json.dumps(exec_detail.jupiter_route) if exec_detail.jupiter_route else None,
                            exec_detail.jupiter_route_hops,
                            json.dumps(exec_detail.jupiter_dexes_used) if exec_detail.jupiter_dexes_used else None,
                            exec_detail.jupiter_quote_in, exec_detail.jupiter_quote_out,
                            exec_detail.jupiter_price_impact_pct, exec_detail.jupiter_route_score,
                            exec_detail.jupiter_no_route_reason, exec_detail.requested_in_amount,
                            exec_detail.requested_out_min, exec_detail.slippage_bps_configured,
                            exec_detail.actual_in_amount, exec_detail.actual_out_amount,
                            exec_detail.effective_price, exec_detail.realized_slippage_bps,
                            exec_detail.price_impact_realized_pct, exec_detail.priority_fee_lamports,
                            exec_detail.compute_units_used, exec_detail.tx_fee_lamports,
                            exec_detail.total_cost_sol, exec_detail.submit_at,
                            exec_detail.confirm_at, exec_detail.send_to_confirm_ms,
                            exec_detail.attempt_number, exec_detail.total_retries,
                            json.dumps(exec_detail.errors) if exec_detail.errors else None,
                            exec_detail.final_status
                        )
                    
                    # Insert token risk data
                    if trade.token_risk:
                        await conn.execute("""
                            INSERT INTO token_risk_data (
                                trade_id, correlation_id, token_mint, captured_at,
                                has_mint_authority, has_freeze_authority,
                                mint_authority_address, freeze_authority_address,
                                is_token_2022, has_transfer_fee, transfer_fee_bps,
                                has_permanent_delegate, permanent_delegate_address,
                                has_non_transferable, extensions, holders_count,
                                top10_holders_pct, top20_holders_pct, dev_wallet_pct,
                                dev_wallet_address, lp_locked_pct, lp_burn_pct,
                                top_lp_holders, rugcheck_score, rugcheck_risk_level,
                                rugcheck_flags, creator_wallet, is_trader_creator,
                                creator_other_tokens, creator_rug_history,
                                has_website, has_twitter, has_telegram, metadata_uri
                            ) VALUES (
                                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                                $13, $14, $15, $16, $17, $18, $19, $20, $21, $22,
                                $23, $24, $25, $26, $27, $28, $29, $30, $31, $32, $33, $34
                            )
                        """,
                            trade_id, trade.correlation_id, trade.token_risk.token_mint,
                            trade.token_risk.captured_at, trade.token_risk.has_mint_authority,
                            trade.token_risk.has_freeze_authority,
                            trade.token_risk.mint_authority_address,
                            trade.token_risk.freeze_authority_address,
                            trade.token_risk.is_token_2022, trade.token_risk.has_transfer_fee,
                            trade.token_risk.transfer_fee_bps, trade.token_risk.has_permanent_delegate,
                            trade.token_risk.permanent_delegate_address,
                            trade.token_risk.has_non_transferable,
                            json.dumps(trade.token_risk.extensions) if trade.token_risk.extensions else None,
                            trade.token_risk.holders_count, trade.token_risk.top10_holders_pct,
                            trade.token_risk.top20_holders_pct, trade.token_risk.dev_wallet_pct,
                            trade.token_risk.dev_wallet_address, trade.token_risk.lp_locked_pct,
                            trade.token_risk.lp_burn_pct,
                            json.dumps(trade.token_risk.top_lp_holders) if trade.token_risk.top_lp_holders else None,
                            trade.token_risk.rugcheck_score, trade.token_risk.rugcheck_risk_level,
                            json.dumps(trade.token_risk.rugcheck_flags) if trade.token_risk.rugcheck_flags else None,
                            trade.token_risk.creator_wallet, trade.token_risk.is_trader_creator,
                            trade.token_risk.creator_other_tokens, trade.token_risk.creator_rug_history,
                            trade.token_risk.has_website, trade.token_risk.has_twitter,
                            trade.token_risk.has_telegram, trade.token_risk.metadata_uri
                        )
                    
                    logger.info("trade_recorded", 
                               trade_id=str(trade_id)[:8],
                               correlation_id=trade.correlation_id[:8],
                               token=trade.token_mint[:8],
                               status=trade.status)
                    
                    return str(trade_id)
                    
        except Exception as e:
            logger.error("trade_record_error", error=str(e), token=trade.token_mint[:8])
            return None

    async def record_skipped_trade(
        self,
        correlation_id: str,
        token_mint: str,
        trader_wallet: str,
        their_signature: str,
        their_sol_amount: Decimal,
        their_dex: str,
        skip_reason: str,
        skip_category: str,
        market_snapshot: Optional[MarketSnapshot] = None,
        filter_thresholds: Optional[Dict] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None
    ):
        """Record a trade we detected but didn't copy."""
        if not self.pool:
            return
        
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO skipped_trades (
                        correlation_id, token_mint, trader_wallet, their_signature,
                        their_sol_amount, their_dex, skip_reason, skip_category,
                        market_cap_usd, liquidity_usd, volume_24h_usd,
                        price_change_1h_pct, txns_1h, token_age_minutes,
                        required_min_mcap, required_min_liquidity, required_min_volume,
                        required_min_age, required_max_pump_pct,
                        error_code, error_message
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                        $15, $16, $17, $18, $19, $20, $21
                    )
                """,
                    correlation_id, token_mint, trader_wallet, their_signature,
                    their_sol_amount, their_dex, skip_reason, skip_category,
                    market_snapshot.market_cap_usd if market_snapshot else None,
                    market_snapshot.liquidity_usd if market_snapshot else None,
                    market_snapshot.volume_24h_usd if market_snapshot else None,
                    market_snapshot.price_change_h1_pct if market_snapshot else None,
                    (market_snapshot.txns_1h_buys or 0) + (market_snapshot.txns_1h_sells or 0) if market_snapshot else None,
                    market_snapshot.token_age_minutes if market_snapshot else None,
                    filter_thresholds.get("min_mcap") if filter_thresholds else None,
                    filter_thresholds.get("min_liquidity") if filter_thresholds else None,
                    filter_thresholds.get("min_volume") if filter_thresholds else None,
                    filter_thresholds.get("min_age") if filter_thresholds else None,
                    filter_thresholds.get("max_pump") if filter_thresholds else None,
                    error_code, error_message
                )
                
            logger.debug("skipped_trade_recorded",
                        correlation_id=correlation_id[:8],
                        token=token_mint[:8],
                        reason=skip_reason)
                        
        except Exception as e:
            logger.error("skipped_trade_record_error", error=str(e))

    async def record_failed_execution(
        self,
        trade_id: Optional[str],
        correlation_id: str,
        token_mint: str,
        execution_type: str,
        method: str,
        error_code: str,
        error_message: str,
        error_category: str,
        attempt_number: int,
        token_balance: Optional[Decimal] = None,
        sol_balance: Optional[Decimal] = None,
        liquidity: Optional[Decimal] = None,
        requested_amount: Optional[Decimal] = None,
        slippage_bps: Optional[int] = None,
        priority_fee: Optional[int] = None
    ):
        """Record a failed execution attempt."""
        if not self.pool:
            return
        
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO failed_executions (
                        trade_id, correlation_id, token_mint, execution_type,
                        method, error_code, error_message, error_category,
                        attempt_number, token_balance_at_attempt,
                        sol_balance_at_attempt, liquidity_at_attempt,
                        requested_amount, slippage_bps, priority_fee
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                """,
                    uuid.UUID(trade_id) if trade_id else None,
                    correlation_id, token_mint, execution_type, method,
                    error_code, error_message, error_category, attempt_number,
                    token_balance, sol_balance, liquidity, requested_amount,
                    slippage_bps, priority_fee
                )
                
        except Exception as e:
            logger.error("failed_execution_record_error", error=str(e))

    # ========================================================================
    # Post-Trade Follow-ups
    # ========================================================================
    
    async def schedule_post_trade_followups(
        self,
        trade_id: str,
        correlation_id: str,
        token_mint: str,
        exit_price_usd: Decimal,
        exit_price_sol: Decimal,
        entry_price_sol: Decimal
    ):
        """Schedule follow-up snapshots after a trade closes."""
        followup_minutes = [1, 3, 5, 10, 30, 60]
        
        async def run_followups():
            exit_at = datetime.now(timezone.utc)
            
            for minutes in followup_minutes:
                await asyncio.sleep(minutes * 60)
                
                try:
                    # Fetch current market data
                    snapshot = await self.fetch_market_snapshot(
                        token_mint,
                        f"follow_up_{minutes}m",
                        minutes_after_event=minutes
                    )
                    
                    if not self.pool:
                        continue
                    
                    # Calculate counterfactuals
                    current_price = snapshot.price_sol or Decimal("0")
                    pnl_if_held_sol = current_price - entry_price_sol if current_price else None
                    pnl_if_held_pct = (
                        ((current_price - entry_price_sol) / entry_price_sol * 100)
                        if current_price and entry_price_sol else None
                    )
                    price_change_since_exit = (
                        ((current_price - exit_price_sol) / exit_price_sol * 100)
                        if current_price and exit_price_sol else None
                    )
                    price_recovered = current_price > exit_price_sol if current_price else None
                    
                    async with self.pool.acquire() as conn:
                        await conn.execute("""
                            INSERT INTO post_trade_followups (
                                trade_id, correlation_id, token_mint,
                                exit_price_usd, exit_price_sol, exit_at,
                                followup_minutes, followup_at,
                                price_usd, price_sol, market_cap_usd, liquidity_usd,
                                pnl_if_held_pct, pnl_if_held_sol,
                                price_change_since_exit_pct, price_recovered
                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
                        """,
                            uuid.UUID(trade_id), correlation_id, token_mint,
                            exit_price_usd, exit_price_sol, exit_at,
                            minutes, datetime.now(timezone.utc),
                            snapshot.price_usd, snapshot.price_sol,
                            snapshot.market_cap_usd, snapshot.liquidity_usd,
                            pnl_if_held_pct, pnl_if_held_sol,
                            price_change_since_exit, price_recovered
                        )
                        
                    logger.debug("followup_recorded",
                                trade_id=trade_id[:8],
                                minutes=minutes,
                                price_recovered=price_recovered)
                                
                except Exception as e:
                    logger.error("followup_error", 
                                trade_id=trade_id[:8],
                                minutes=minutes,
                                error=str(e))
        
        # Start background task for follow-ups
        task = asyncio.create_task(run_followups())
        self._follow_up_tasks[trade_id] = task

    async def update_trade_exit(
        self,
        correlation_id: str,
        exit_reason: str,
        exit_signature: str,
        sol_received: Decimal,
        pnl_sol: Decimal,
        pnl_pct: Decimal,
        exit_mcap: Optional[Decimal] = None,
        time_in_trade_sec: Optional[int] = None,
        cupsey_still_holding: Optional[bool] = None
    ):
        """Update a trade record with exit information."""
        if not self.pool:
            return
        
        try:
            async with self.pool.acquire() as conn:
                # Get the trade
                row = await conn.fetchrow(
                    "SELECT id, token_mint, our_sol_amount FROM trades WHERE correlation_id = $1",
                    correlation_id
                )
                
                if not row:
                    logger.warning("trade_not_found_for_exit", correlation_id=correlation_id[:8])
                    return
                
                trade_id = row["id"]
                token_mint = row["token_mint"]
                entry_sol = row["our_sol_amount"] or Decimal("0")
                
                # Update trade
                await conn.execute("""
                    UPDATE trades SET
                        status = 'closed',
                        closed_at = NOW(),
                        exit_reason = $1,
                        exit_pnl_pct = $2,
                        exit_mcap_usd = $3,
                        exit_time_in_trade_sec = $4,
                        cupsey_still_holding = $5,
                        realized_pnl_sol = $6,
                        realized_pnl_pct = $7
                    WHERE id = $8
                """,
                    exit_reason, pnl_pct, exit_mcap, time_in_trade_sec,
                    cupsey_still_holding, pnl_sol, pnl_pct, trade_id
                )
                
                # Fetch exit snapshot
                exit_snapshot = await self.fetch_market_snapshot(token_mint, "exit")
                
                # Insert exit snapshot
                await conn.execute("""
                    INSERT INTO market_snapshots (
                        trade_id, correlation_id, token_mint, snapshot_type,
                        snapshot_at, data_source, price_usd, price_sol,
                        market_cap_usd, liquidity_usd
                    ) VALUES ($1, $2, $3, 'exit', NOW(), $4, $5, $6, $7, $8)
                """,
                    trade_id, correlation_id, token_mint,
                    exit_snapshot.data_source, exit_snapshot.price_usd,
                    exit_snapshot.price_sol, exit_snapshot.market_cap_usd,
                    exit_snapshot.liquidity_usd
                )
                
                # Schedule post-trade follow-ups
                if exit_snapshot.price_usd and exit_snapshot.price_sol:
                    await self.schedule_post_trade_followups(
                        str(trade_id), correlation_id, token_mint,
                        exit_snapshot.price_usd, exit_snapshot.price_sol,
                        entry_sol
                    )
                
                logger.info("trade_exit_recorded",
                           trade_id=str(trade_id)[:8],
                           exit_reason=exit_reason,
                           pnl_pct=f"{pnl_pct:+.2f}%")
                           
        except Exception as e:
            logger.error("trade_exit_record_error", error=str(e))


# ============================================================================
# Global Instance
# ============================================================================

# Singleton instance
_telemetry: Optional[TradeTelemetry] = None


def get_telemetry() -> TradeTelemetry:
    """Get or create the global telemetry instance."""
    global _telemetry
    if _telemetry is None:
        _telemetry = TradeTelemetry()
    return _telemetry


async def init_telemetry() -> TradeTelemetry:
    """Initialize and start the telemetry system."""
    telemetry = get_telemetry()
    await telemetry.start()
    return telemetry
