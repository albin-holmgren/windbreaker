"""
Pulse Sniper - Buy tokens when they hit quality thresholds.

Instead of copy trading, this monitors pump.fun tokens and buys
when they pass quality filters (similar to Axiom's Pulse feature).
"""

import asyncio
import aiohttp
import time
from dataclasses import dataclass
from typing import Optional, Set
import structlog
import os

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient

logger = structlog.get_logger()

# Pump.fun bonding curve program
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPPORTAL_API = "https://pumpportal.fun/api"

# Token addresses
SOL_MINT = "So11111111111111111111111111111111111111112"


@dataclass
class TokenMetrics:
    """Token metrics for filtering."""
    mint: str
    name: str
    symbol: str
    market_cap_usd: float
    liquidity_usd: float
    volume_24h_usd: float
    age_minutes: float
    top10_holders_pct: float
    dev_holdings_pct: float
    bundlers_pct: float
    holders_count: int
    price_usd: float


@dataclass
class SniperConfig:
    """Configuration for pulse sniper."""
    # Minimum thresholds
    min_market_cap_usd: float = 20000
    min_volume_24h_usd: float = 30000
    min_liquidity_usd: float = 10000
    min_holders_count: int = 100
    min_age_minutes: float = 3
    
    # Maximum thresholds (safety)
    max_top10_holders_pct: float = 30
    max_dev_holdings_pct: float = 30
    max_bundlers_pct: float = 30
    max_market_cap_usd: float = 500000  # Don't buy if too big
    
    # Trade settings
    buy_amount_sol: float = 0.05
    max_positions: int = 3
    slippage_bps: int = 1500  # 15%
    
    # Position management
    take_profit_pct: float = 50
    stop_loss_pct: float = -35
    trailing_stop_pct: float = 25
    
    # Scan settings
    scan_interval_seconds: float = 5


class PulseSniper:
    """
    Monitors pump.fun tokens and buys when they hit quality thresholds.
    """
    
    def __init__(
        self,
        rpc: AsyncClient,
        wallet: Keypair,
        config: SniperConfig,
        position_manager=None
    ):
        self.rpc = rpc
        self.wallet = wallet
        self.config = config
        self.position_manager = position_manager
        
        self.session: Optional[aiohttp.ClientSession] = None
        self.running = False
        
        # Track tokens we've already bought or evaluated
        self.bought_tokens: Set[str] = set()
        self.evaluated_tokens: dict[str, float] = {}  # mint -> last_eval_time
        
        # Cache for token info
        self.token_cache: dict[str, tuple[TokenMetrics, float]] = {}  # mint -> (metrics, timestamp)
        
    async def start(self):
        """Start the pulse sniper."""
        self.session = aiohttp.ClientSession()
        self.running = True
        
        logger.info(
            "pulse_sniper_started",
            min_mcap=f"${self.config.min_market_cap_usd:,.0f}",
            min_volume=f"${self.config.min_volume_24h_usd:,.0f}",
            min_holders=self.config.min_holders_count,
            max_top10=f"{self.config.max_top10_holders_pct}%",
            max_dev=f"{self.config.max_dev_holdings_pct}%",
            buy_amount=f"{self.config.buy_amount_sol} SOL"
        )
        
        # Start scanning loop
        asyncio.create_task(self._scan_loop())
        
        # Start position monitoring if we have a position manager
        if self.position_manager:
            asyncio.create_task(self.position_manager.start_monitoring())
    
    async def stop(self):
        """Stop the pulse sniper."""
        self.running = False
        if self.session:
            await self.session.close()
        logger.info("pulse_sniper_stopped")
    
    async def _scan_loop(self):
        """Main scanning loop - fetch trending tokens and evaluate."""
        while self.running:
            try:
                # Get trending/new pump.fun tokens from DexScreener
                tokens = await self._fetch_pumpfun_tokens()
                
                for token in tokens:
                    if token.mint in self.bought_tokens:
                        continue
                    
                    # Check if we recently evaluated this token
                    last_eval = self.evaluated_tokens.get(token.mint, 0)
                    if time.time() - last_eval < 60:  # Re-evaluate every 60 seconds
                        continue
                    
                    self.evaluated_tokens[token.mint] = time.time()
                    
                    # Check if token passes all filters
                    passed, reason = self._check_filters(token)
                    
                    if passed:
                        logger.info(
                            "token_passed_filters",
                            token=token.symbol,
                            mint=token.mint[:8],
                            market_cap=f"${token.market_cap_usd:,.0f}",
                            volume=f"${token.volume_24h_usd:,.0f}",
                            holders=token.holders_count,
                            top10=f"{token.top10_holders_pct:.1f}%",
                            dev=f"{token.dev_holdings_pct:.1f}%",
                            age=f"{token.age_minutes:.1f}m"
                        )
                        
                        # Check if we can open more positions
                        current_positions = len(self.position_manager.positions) if self.position_manager else 0
                        if current_positions >= self.config.max_positions:
                            logger.info("max_positions_reached", current=current_positions, max=self.config.max_positions)
                            continue
                        
                        # Execute buy
                        success = await self._execute_buy(token)
                        if success:
                            self.bought_tokens.add(token.mint)
                    else:
                        logger.debug(
                            "token_filtered",
                            token=token.symbol[:8] if token.symbol else token.mint[:8],
                            reason=reason
                        )
                
                await asyncio.sleep(self.config.scan_interval_seconds)
                
            except Exception as e:
                logger.error("scan_loop_error", error=str(e))
                await asyncio.sleep(5)
    
    async def _fetch_pumpfun_tokens(self) -> list[TokenMetrics]:
        """Fetch pump.fun tokens from DexScreener."""
        tokens = []
        
        try:
            # Get pump.fun tokens from DexScreener
            url = "https://api.dexscreener.com/latest/dex/search?q=pump.fun"
            
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs", [])
                    
                    for pair in pairs[:50]:  # Check top 50
                        if pair.get("chainId") != "solana":
                            continue
                        if "pump" not in pair.get("dexId", "").lower():
                            continue
                        
                        mint = pair.get("baseToken", {}).get("address", "")
                        if not mint:
                            continue
                        
                        # Get holder info
                        top10_pct, dev_pct, holders, bundlers_pct = await self._get_holder_info(mint)
                        
                        # Calculate age
                        created_at = pair.get("pairCreatedAt", 0)
                        age_minutes = (time.time() * 1000 - created_at) / 60000 if created_at else 0
                        
                        metrics = TokenMetrics(
                            mint=mint,
                            name=pair.get("baseToken", {}).get("name", ""),
                            symbol=pair.get("baseToken", {}).get("symbol", ""),
                            market_cap_usd=pair.get("marketCap", 0) or pair.get("fdv", 0) or 0,
                            liquidity_usd=pair.get("liquidity", {}).get("usd", 0) or 0,
                            volume_24h_usd=pair.get("volume", {}).get("h24", 0) or 0,
                            age_minutes=age_minutes,
                            top10_holders_pct=top10_pct,
                            dev_holdings_pct=dev_pct,
                            bundlers_pct=bundlers_pct,
                            holders_count=holders,
                            price_usd=float(pair.get("priceUsd", 0) or 0)
                        )
                        tokens.append(metrics)
            
            logger.debug("fetched_tokens", count=len(tokens))
            
        except Exception as e:
            logger.error("fetch_tokens_error", error=str(e))
        
        return tokens
    
    async def _get_holder_info(self, mint: str) -> tuple[float, float, int, float]:
        """Get holder distribution info using RugCheck API."""
        try:
            url = f"https://api.rugcheck.xyz/v1/tokens/{mint}/report"
            
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    # Get top holders percentage
                    top_holders = data.get("topHolders", [])
                    top10_pct = sum(h.get("pct", 0) for h in top_holders[:10]) if top_holders else 0
                    
                    # Get creator/dev holdings
                    creator = data.get("creator", {})
                    dev_pct = creator.get("pct", 0) if creator else 0
                    
                    # Get total holders
                    holders = data.get("totalHolders", 0)
                    
                    # Get bundlers percentage (if available)
                    risks = data.get("risks", [])
                    bundlers_pct = 0
                    for risk in risks:
                        if "bundle" in risk.get("name", "").lower():
                            # Try to extract percentage from description
                            bundlers_pct = 10  # Default assumption if bundling detected
                    
                    return top10_pct, dev_pct, holders, bundlers_pct
            
            return 0, 0, 0, 0
            
        except Exception as e:
            logger.debug("holder_info_error", mint=mint[:8], error=str(e))
            return 0, 0, 0, 0
    
    def _check_filters(self, token: TokenMetrics) -> tuple[bool, str]:
        """Check if token passes all filters."""
        
        # Minimum checks
        if token.market_cap_usd < self.config.min_market_cap_usd:
            return False, f"mcap_too_low (${token.market_cap_usd:,.0f} < ${self.config.min_market_cap_usd:,.0f})"
        
        if token.market_cap_usd > self.config.max_market_cap_usd:
            return False, f"mcap_too_high (${token.market_cap_usd:,.0f} > ${self.config.max_market_cap_usd:,.0f})"
        
        if token.volume_24h_usd < self.config.min_volume_24h_usd:
            return False, f"volume_too_low (${token.volume_24h_usd:,.0f} < ${self.config.min_volume_24h_usd:,.0f})"
        
        if token.liquidity_usd < self.config.min_liquidity_usd:
            return False, f"liquidity_too_low (${token.liquidity_usd:,.0f} < ${self.config.min_liquidity_usd:,.0f})"
        
        if token.holders_count < self.config.min_holders_count:
            return False, f"holders_too_few ({token.holders_count} < {self.config.min_holders_count})"
        
        if token.age_minutes < self.config.min_age_minutes:
            return False, f"too_new ({token.age_minutes:.1f}m < {self.config.min_age_minutes}m)"
        
        # Maximum checks (safety)
        if token.top10_holders_pct > self.config.max_top10_holders_pct:
            return False, f"top10_too_high ({token.top10_holders_pct:.1f}% > {self.config.max_top10_holders_pct}%)"
        
        if token.dev_holdings_pct > self.config.max_dev_holdings_pct:
            return False, f"dev_holdings_too_high ({token.dev_holdings_pct:.1f}% > {self.config.max_dev_holdings_pct}%)"
        
        if token.bundlers_pct > self.config.max_bundlers_pct:
            return False, f"bundlers_too_high ({token.bundlers_pct:.1f}% > {self.config.max_bundlers_pct}%)"
        
        return True, "passed"
    
    async def _execute_buy(self, token: TokenMetrics) -> bool:
        """Execute buy via PumpPortal API."""
        try:
            logger.info(
                "executing_snipe_buy",
                token=token.symbol,
                mint=token.mint[:8],
                amount_sol=self.config.buy_amount_sol
            )
            
            # Use PumpPortal API for the trade
            url = f"{PUMPPORTAL_API}/trade-local"
            
            payload = {
                "publicKey": str(self.wallet.pubkey()),
                "action": "buy",
                "mint": token.mint,
                "amount": self.config.buy_amount_sol,  # SOL amount
                "denominatedInSol": "true",
                "slippage": self.config.slippage_bps / 100,  # Convert to percentage
                "priorityFee": 0.001,  # 0.001 SOL priority fee
                "pool": "pump"
            }
            
            async with self.session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    tx_data = await resp.read()
                    
                    # Sign and send transaction
                    from solders.transaction import VersionedTransaction
                    tx = VersionedTransaction.from_bytes(tx_data)
                    signed_tx = VersionedTransaction(tx.message, [self.wallet])
                    
                    # Send transaction
                    result = await self.rpc.send_transaction(signed_tx)
                    signature = str(result.value)
                    
                    logger.info(
                        "snipe_buy_success",
                        token=token.symbol,
                        mint=token.mint[:8],
                        signature=signature[:16],
                        amount_sol=self.config.buy_amount_sol
                    )
                    
                    # Add position to manager
                    if self.position_manager:
                        await self.position_manager.add_position(
                            token_mint=token.mint,
                            entry_sol=self.config.buy_amount_sol,
                            token_amount=0,  # Will be fetched
                            entry_price=token.price_usd,
                            dex="pump.fun"
                        )
                    
                    return True
                else:
                    error_text = await resp.text()
                    logger.error(
                        "snipe_buy_failed",
                        token=token.symbol,
                        status=resp.status,
                        error=error_text[:200]
                    )
                    return False
                    
        except Exception as e:
            logger.error("snipe_buy_error", token=token.symbol, error=str(e))
            return False


async def main():
    """Main entry point for pulse sniper."""
    from dotenv import load_dotenv
    import base58
    
    load_dotenv()
    
    # Load config from environment
    rpc_url = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
    private_key = os.getenv("WALLET_PRIVATE_KEY_BASE58")
    
    if not private_key:
        logger.error("WALLET_PRIVATE_KEY_BASE58 not set")
        return
    
    # Create wallet
    wallet = Keypair.from_bytes(base58.b58decode(private_key))
    logger.info("wallet_loaded", pubkey=str(wallet.pubkey())[:8] + "...")
    
    # Create RPC client
    rpc = AsyncClient(rpc_url)
    
    # Create config from environment
    config = SniperConfig(
        min_market_cap_usd=float(os.getenv("MIN_MARKET_CAP_USD", "20000")),
        min_volume_24h_usd=float(os.getenv("MIN_VOLUME_24H_USD", "30000")),
        min_liquidity_usd=float(os.getenv("MIN_LIQUIDITY_USD", "10000")),
        min_holders_count=int(os.getenv("MIN_HOLDERS_COUNT", "100")),
        min_age_minutes=float(os.getenv("MIN_TOKEN_AGE_MINUTES", "3")),
        max_top10_holders_pct=float(os.getenv("MAX_TOP10_HOLDERS_PCT", "30")),
        max_dev_holdings_pct=float(os.getenv("MAX_DEV_HOLDINGS_PCT", "30")),
        max_bundlers_pct=float(os.getenv("MAX_BUNDLERS_PCT", "30")),
        max_market_cap_usd=float(os.getenv("MAX_MARKET_CAP_USD", "500000")),
        buy_amount_sol=float(os.getenv("SNIPE_AMOUNT_SOL", "0.05")),
        max_positions=int(os.getenv("MAX_POSITIONS", "3")),
        slippage_bps=int(os.getenv("SLIPPAGE_BPS", "1500")),
        take_profit_pct=float(os.getenv("TAKE_PROFIT_PCT", "50")),
        stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "-35")),
        trailing_stop_pct=float(os.getenv("TRAILING_STOP_PCT", "25")),
        scan_interval_seconds=float(os.getenv("SCAN_INTERVAL_SECONDS", "5"))
    )
    
    # Import position manager
    from src.position_manager import PositionManager
    
    position_manager = PositionManager(
        rpc=rpc,
        wallet=wallet,
        take_profit_pct=config.take_profit_pct,
        stop_loss_pct=config.stop_loss_pct,
        trailing_stop_pct=config.trailing_stop_pct,
        check_interval=10
    )
    
    # Create and start sniper
    sniper = PulseSniper(
        rpc=rpc,
        wallet=wallet,
        config=config,
        position_manager=position_manager
    )
    
    await sniper.start()
    
    # Keep running
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("shutting_down")
        await sniper.stop()


if __name__ == "__main__":
    asyncio.run(main())
