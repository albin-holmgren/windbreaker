"""
Wallet Monitor - Tracks transactions from target wallets.
Uses WebSocket subscriptions for INSTANT detection + polling as fallback.
"""

import asyncio
import aiohttp
import json
import os
import time
from typing import List, Dict, Set, Optional, Callable, Any
from dataclasses import dataclass
from datetime import datetime
import structlog

logger = structlog.get_logger(__name__)


def get_websocket_url(rpc_url: str) -> str:
    """Convert HTTP RPC URL to WebSocket URL."""
    if "helius-rpc.com" in rpc_url:
        return rpc_url.replace("https://", "wss://").replace("http://", "ws://")
    elif "quicknode" in rpc_url.lower():
        return rpc_url.replace("https://", "wss://").replace("http://", "ws://")
    else:
        return rpc_url.replace("https://", "wss://").replace("http://", "ws://")


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
    Uses WebSocket for INSTANT detection + polling as fallback.
    """
    
    def __init__(
        self,
        rpc_url: str,
        target_wallets: List[str],
        poll_interval_ms: int = 3000,
        on_transaction: Optional[Callable[[WalletTransaction], Any]] = None,
        use_websocket: bool = True
    ):
        rpc_urls: List[str] = [u.strip() for u in (rpc_url or "").split(",") if u.strip()]
        secondary = os.getenv("RPC_URL_SECONDARY", "")
        if secondary:
            rpc_urls.extend([u.strip() for u in secondary.split(",") if u.strip()])
        self.rpc_urls: List[str] = []
        for u in rpc_urls:
            if u not in self.rpc_urls:
                self.rpc_urls.append(u)
        self.rpc_url = self.rpc_urls[0] if self.rpc_urls else rpc_url
        self.ws_url = get_websocket_url(self.rpc_url)
        self.target_wallets = target_wallets
        self.poll_interval = poll_interval_ms / 1000.0
        self.on_transaction = on_transaction
        self.use_websocket = use_websocket and os.getenv('USE_WEBSOCKET', 'true').lower() == 'true'
        
        # Track seen signatures to avoid duplicates
        self.seen_signatures: Dict[str, Set[str]] = {w: set() for w in target_wallets}
        self.session: Optional[aiohttp.ClientSession] = None
        self.ws_connection = None
        self.running = False
        self._poll_count = 0
        self._ws_connected = False
        self._subscription_ids: Dict[str, int] = {}  # wallet -> subscription_id
        self._pending_signatures: Dict[str, Dict[str, tuple[float, int]]] = {w: {} for w in target_wallets}
        self._fetch_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=int(os.getenv("TX_FETCH_QUEUE_SIZE", "5000")))
        self._fetch_workers: List[asyncio.Task] = []
        self._tx_fetch_max_age_sec = float(os.getenv("TX_FETCH_MAX_AGE_SEC", "12"))
        self._tx_fetch_max_attempts = int(os.getenv("TX_FETCH_MAX_ATTEMPTS", "8"))
        self._tx_fetch_workers = int(os.getenv("TX_FETCH_WORKERS", "2"))
        
    async def start(self) -> None:
        """Start the wallet monitor with WebSocket + polling fallback."""
        self.session = aiohttp.ClientSession()
        self.running = True
        
        logger.info(
            "wallet_monitor_started",
            wallets=len(self.target_wallets),
            poll_interval_ms=int(self.poll_interval * 1000),
            websocket_enabled=self.use_websocket,
            ws_url=self.ws_url[:50] + "..." if len(self.ws_url) > 50 else self.ws_url
        )
        
        # Initialize seen signatures with recent transactions
        await self._initialize_seen_signatures()

        for i in range(max(1, self._tx_fetch_workers)):
            self._fetch_workers.append(asyncio.create_task(self._tx_fetch_worker(i)))
        
        # Start WebSocket listener in background (if enabled)
        if self.use_websocket:
            asyncio.create_task(self._websocket_listener())
        
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
        for t in self._fetch_workers:
            t.cancel()
        if self._fetch_workers:
            await asyncio.gather(*self._fetch_workers, return_exceptions=True)
        if self.ws_connection:
            await self.ws_connection.close()
        if self.session:
            await self.session.close()
        logger.info("wallet_monitor_stopped")
    
    async def _websocket_listener(self) -> None:
        """WebSocket listener for INSTANT transaction detection."""
        reconnect_delay = 1
        max_reconnect_delay = 30
        
        while self.running:
            try:
                logger.info("websocket_connecting", url=self.ws_url[:60] + "...")
                
                async with aiohttp.ClientSession() as ws_session:
                    async with ws_session.ws_connect(
                        self.ws_url,
                        heartbeat=30,
                        receive_timeout=60
                    ) as ws:
                        self.ws_connection = ws
                        self._ws_connected = True
                        reconnect_delay = 1  # Reset on successful connect
                        
                        logger.info("websocket_connected", wallets=len(self.target_wallets))
                        
                        # Subscribe to all wallet accounts
                        for i, wallet in enumerate(self.target_wallets):
                            subscribe_msg = {
                                "jsonrpc": "2.0",
                                "id": i + 1,
                                "method": "logsSubscribe",
                                "params": [
                                    {"mentions": [wallet]},
                                    {"commitment": "confirmed"}
                                ]
                            }
                            await ws.send_json(subscribe_msg)
                            logger.info("websocket_subscribed", wallet=wallet[:8])
                        
                        # Listen for messages
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._handle_ws_message(msg.data)
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                logger.warning("websocket_error", error=str(ws.exception()))
                                break
                            elif msg.type == aiohttp.WSMsgType.CLOSED:
                                logger.warning("websocket_closed")
                                break
                                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(
                    "websocket_connection_error",
                    error=str(e),
                    reconnect_in=reconnect_delay
                )
            
            self._ws_connected = False
            
            if self.running:
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
    
    async def _handle_ws_message(self, data: str) -> None:
        """Handle incoming WebSocket message - INSTANT processing."""
        try:
            msg = json.loads(data)
            
            # Check if it's a subscription confirmation
            if "result" in msg and isinstance(msg["result"], int):
                sub_id = msg["result"]
                req_id = msg.get("id", 0)
                if req_id > 0 and req_id <= len(self.target_wallets):
                    wallet = self.target_wallets[req_id - 1]
                    self._subscription_ids[wallet] = sub_id
                return
            
            # Check if it's a notification
            if "method" in msg and msg["method"] == "logsNotification":
                params = msg.get("params", {})
                result = params.get("result", {})
                value = result.get("value", {})
                
                signature = value.get("signature")
                logs = value.get("logs", [])
                err = value.get("err")
                
                if not signature:
                    return
                
                # Check which wallet this is for (from logs)
                matched_wallet = None
                for wallet in self.target_wallets:
                    if wallet in str(logs):
                        matched_wallet = wallet
                        break
                
                # If no match in logs, check all wallets
                if not matched_wallet:
                    for wallet in self.target_wallets:
                        if signature not in self.seen_signatures.get(wallet, set()):
                            matched_wallet = wallet
                            break
                
                if not matched_wallet:
                    return
                
                # Skip if already seen
                if signature in self.seen_signatures.get(matched_wallet, set()):
                    return
                
                logger.info(
                    "websocket_tx_detected",
                    wallet=matched_wallet[:8],
                    signature=signature[:16],
                    has_error=err is not None
                )
                
                # Only process successful transactions
                if err is None:
                    await self._enqueue_signature(matched_wallet, signature, "ws")
                                
        except json.JSONDecodeError:
            logger.debug("websocket_invalid_json")
        except Exception as e:
            logger.warning("websocket_message_error", error=str(e))
    
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

        data = await self._rpc_post(payload, timeout_sec=10)
        if not data:
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

        data = await self._rpc_post(payload, timeout_sec=6)
        if not data:
            return None
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
        pending = self._pending_signatures.get(wallet, {})
        new_count = sum(1 for sig in signatures if sig not in self.seen_signatures[wallet] and sig not in pending)
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

            await self._enqueue_signature(wallet, sig, "poll")

    async def _rpc_post(self, payload: Dict[str, Any], timeout_sec: float) -> Optional[Dict[str, Any]]:
        if not self.session:
            return None

        last_error: Optional[str] = None
        for url in self.rpc_urls or [self.rpc_url]:
            try:
                async with self.session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout_sec)
                ) as resp:
                    if resp.status != 200:
                        last_error = f"http_{resp.status}"
                        continue
                    return await resp.json()
            except asyncio.TimeoutError:
                last_error = "timeout"
                continue
            except Exception as e:
                last_error = str(e)
                continue

        if last_error:
            logger.debug("rpc_post_failed", error=last_error)
        return None

    async def _enqueue_signature(self, wallet: str, signature: str, source: str) -> None:
        if signature in self.seen_signatures.get(wallet, set()):
            return
        pending = self._pending_signatures.setdefault(wallet, {})
        if signature in pending:
            return
        pending[signature] = (time.monotonic(), 0)
        try:
            self._fetch_queue.put_nowait((wallet, signature))
        except asyncio.QueueFull:
            pending.pop(signature, None)
            logger.warning("tx_fetch_queue_full", wallet=wallet[:8], signature=signature[:16], source=source)

    async def _requeue_signature(self, wallet: str, signature: str, delay_sec: float) -> None:
        await asyncio.sleep(delay_sec)
        if not self.running:
            return
        pending = self._pending_signatures.get(wallet, {})
        if signature not in pending:
            return
        try:
            self._fetch_queue.put_nowait((wallet, signature))
        except asyncio.QueueFull:
            logger.warning("tx_fetch_queue_full", wallet=wallet[:8], signature=signature[:16])

    async def _tx_fetch_worker(self, worker_id: int) -> None:
        while self.running:
            wallet, signature = await self._fetch_queue.get()
            try:
                pending = self._pending_signatures.get(wallet, {})
                state = pending.get(signature)
                if not state:
                    continue
                first_seen, attempts = state
                attempts += 1
                pending[signature] = (first_seen, attempts)

                tx_data = await self._get_transaction(signature)
                if tx_data:
                    pending.pop(signature, None)
                    self.seen_signatures.setdefault(wallet, set()).add(signature)
                    tx = WalletTransaction(
                        signature=signature,
                        wallet=wallet,
                        timestamp=tx_data.get("blockTime", 0),
                        slot=tx_data.get("slot", 0),
                        success=tx_data.get("meta", {}).get("err") is None,
                        raw_tx=tx_data
                    )
                    logger.info(
                        "new_transaction_detected",
                        wallet=wallet[:8] + "...",
                        signature=signature[:16] + "...",
                        success=tx.success
                    )
                    if self.on_transaction and tx.success:
                        try:
                            await self.on_transaction(tx)
                        except Exception as e:
                            logger.error("transaction_callback_error", error=str(e))
                    continue

                age = time.monotonic() - first_seen
                if age > self._tx_fetch_max_age_sec or attempts >= self._tx_fetch_max_attempts:
                    pending.pop(signature, None)
                    logger.warning(
                        "ws_tx_fetch_failed",
                        wallet=wallet[:8],
                        signature=signature[:16],
                        attempts=attempts,
                        age_sec=round(age, 3),
                        worker=worker_id
                    )
                    continue

                delay = min(0.15 * (2 ** (attempts - 1)), 1.5)
                asyncio.create_task(self._requeue_signature(wallet, signature, delay))
            finally:
                self._fetch_queue.task_done()
    
    def add_wallet(self, wallet: str) -> None:
        """Add a new wallet to monitor."""
        if wallet not in self.target_wallets:
            self.target_wallets.append(wallet)
            self.seen_signatures[wallet] = set()
            self._pending_signatures[wallet] = {}
            logger.info("wallet_added", wallet=wallet[:8] + "...")
    
    def remove_wallet(self, wallet: str) -> None:
        """Remove a wallet from monitoring."""
        if wallet in self.target_wallets:
            self.target_wallets.remove(wallet)
            del self.seen_signatures[wallet]
            self._pending_signatures.pop(wallet, None)
            logger.info("wallet_removed", wallet=wallet[:8] + "...")
