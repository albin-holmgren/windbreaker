"""
Wallet Monitor - Tracks transactions from target wallets.
Polls Helius RPC for recent transactions and detects new ones.
"""

import asyncio
import aiohttp
from typing import List, Dict, Set, Optional, Callable, Any
from dataclasses import dataclass
from datetime import datetime
import structlog

logger = structlog.get_logger(__name__)


@dataclass
class WalletTransaction:
    """Represents a transaction from a monitored wallet."""
    signature: str
    wallet: str
    timestamp: int
    slot: int
    success: bool
    raw_tx: Dict[str, Any]


class WalletMonitor:
    """
    Monitors target wallets for new transactions.
    Uses polling to detect new transactions in real-time.
    """
    
    def __init__(
        self,
        rpc_url: str,
        target_wallets: List[str],
        poll_interval_ms: int = 3000,
        on_transaction: Optional[Callable[[WalletTransaction], Any]] = None
    ):
        self.rpc_url = rpc_url
        self.target_wallets = target_wallets
        self.poll_interval = poll_interval_ms / 1000.0
        self.on_transaction = on_transaction
        
        # Track seen signatures to avoid duplicates
        self.seen_signatures: Dict[str, Set[str]] = {w: set() for w in target_wallets}
        self.session: Optional[aiohttp.ClientSession] = None
        self.running = False
        self._poll_count = 0
        
    async def start(self) -> None:
        """Start the wallet monitor."""
        self.session = aiohttp.ClientSession()
        self.running = True
        
        logger.info(
            "wallet_monitor_started",
            wallets=len(self.target_wallets),
            poll_interval_ms=int(self.poll_interval * 1000)
        )
        
        # Initialize seen signatures with recent transactions
        await self._initialize_seen_signatures()
        
        # Start polling loop
        while self.running:
            try:
                await self._poll_all_wallets()
                self._poll_count += 1
                
                # Log heartbeat every 60 polls (~1 min at 1s interval)
                if self._poll_count % 60 == 0:
                    logger.info(
                        "wallet_monitor_heartbeat",
                        poll_count=self._poll_count,
                        wallets=len(self.target_wallets),
                        seen_sigs={w[:8]: len(s) for w, s in self.seen_signatures.items()}
                    )
            except Exception as e:
                logger.error("poll_error", error=str(e), error_type=type(e).__name__)
            
            await asyncio.sleep(self.poll_interval)
    
    async def stop(self) -> None:
        """Stop the wallet monitor."""
        self.running = False
        if self.session:
            await self.session.close()
        logger.info("wallet_monitor_stopped")
    
    async def _initialize_seen_signatures(self) -> None:
        """Load recent signatures to avoid copying old transactions."""
        for wallet in self.target_wallets:
            try:
                signatures = await self._get_recent_signatures(wallet, limit=20)
                self.seen_signatures[wallet] = set(signatures)
                logger.info(
                    "initialized_wallet",
                    wallet=wallet[:8] + "...",
                    recent_txs=len(signatures)
                )
            except Exception as e:
                logger.error("init_wallet_failed", wallet=wallet[:8], error=str(e))
    
    async def _get_recent_signatures(self, wallet: str, limit: int = 10) -> List[str]:
        """Get recent transaction signatures for a wallet."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [
                wallet,
                {"limit": limit}
            ]
        }
        
        try:
            async with self.session.post(self.rpc_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.warning("rpc_http_error", wallet=wallet[:8], status=resp.status)
                    return []
                data = await resp.json()
        except asyncio.TimeoutError:
            logger.warning("rpc_timeout", wallet=wallet[:8])
            return []
        except Exception as e:
            logger.warning("rpc_request_error", wallet=wallet[:8], error=str(e), error_type=type(e).__name__)
            return []
            
        if "error" in data:
            logger.warning("rpc_error", wallet=wallet[:8], error=data["error"])
            return []
            
        if "result" not in data:
            logger.debug("rpc_no_result", wallet=wallet[:8], response_keys=list(data.keys()))
            return []
        
        return [tx["signature"] for tx in data["result"]]
    
    async def _get_transaction(self, signature: str) -> Optional[Dict]:
        """Get full transaction details."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0
                }
            ]
        }
        
        async with self.session.post(self.rpc_url, json=payload) as resp:
            data = await resp.json()
        
        return data.get("result")
    
    async def _poll_all_wallets(self) -> None:
        """Poll all target wallets for new transactions."""
        for i, wallet in enumerate(self.target_wallets):
            try:
                await self._poll_wallet(wallet)
            except Exception as e:
                logger.warning("poll_wallet_failed", wallet=wallet[:8], error=str(e), error_type=type(e).__name__)
            
            # Add delay between wallets to avoid RPC rate limiting (except after last wallet)
            if i < len(self.target_wallets) - 1:
                await asyncio.sleep(0.5)
    
    async def _poll_wallet(self, wallet: str) -> None:
        """Poll a single wallet for new transactions."""
        # Increased from 5 to 20 - active traders can do many transactions between polls
        signatures = await self._get_recent_signatures(wallet, limit=20)
        
        # Log polling status periodically (every poll shows we're alive)
        new_count = sum(1 for sig in signatures if sig not in self.seen_signatures[wallet])
        if new_count > 0:
            logger.info(
                "poll_found_new",
                wallet=wallet[:8],
                total_sigs=len(signatures),
                new_sigs=new_count
            )
        
        for sig in signatures:
            if sig in self.seen_signatures[wallet]:
                continue
            
            # New transaction detected!
            self.seen_signatures[wallet].add(sig)
            
            # Get full transaction details
            tx_data = await self._get_transaction(sig)
            if not tx_data:
                continue
            
            # Create transaction object
            tx = WalletTransaction(
                signature=sig,
                wallet=wallet,
                timestamp=tx_data.get("blockTime", 0),
                slot=tx_data.get("slot", 0),
                success=tx_data.get("meta", {}).get("err") is None,
                raw_tx=tx_data
            )
            
            logger.info(
                "new_transaction_detected",
                wallet=wallet[:8] + "...",
                signature=sig[:16] + "...",
                success=tx.success
            )
            
            # Call the callback if provided
            if self.on_transaction and tx.success:
                try:
                    await self.on_transaction(tx)
                except Exception as e:
                    logger.error("transaction_callback_error", error=str(e))
    
    def add_wallet(self, wallet: str) -> None:
        """Add a new wallet to monitor."""
        if wallet not in self.target_wallets:
            self.target_wallets.append(wallet)
            self.seen_signatures[wallet] = set()
            logger.info("wallet_added", wallet=wallet[:8] + "...")
    
    def remove_wallet(self, wallet: str) -> None:
        """Remove a wallet from monitoring."""
        if wallet in self.target_wallets:
            self.target_wallets.remove(wallet)
            del self.seen_signatures[wallet]
            logger.info("wallet_removed", wallet=wallet[:8] + "...")
