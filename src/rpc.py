"""
RPC client wrapper for Solana.
Supports Helius/QuickNode with rate limiting and backoff.
"""

import asyncio
import time
from typing import Optional, Dict, Any, List
import aiohttp
from solders.rpc.responses import GetBalanceResp, SendTransactionResp
from solders.transaction import VersionedTransaction
from solders.signature import Signature
from solders.pubkey import Pubkey
import structlog

from .config import Config, BACKOFF_BASE_SECONDS, BACKOFF_MAX_SECONDS, MAX_REQUESTS_PER_SECOND

logger = structlog.get_logger()


class RateLimiter:
    """Simple rate limiter using token bucket algorithm."""
    
    def __init__(self, max_per_second: float):
        self.max_per_second = max_per_second
        self.tokens = max_per_second
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()
    
    async def acquire(self) -> None:
        """Acquire a token, waiting if necessary."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_update
            self.tokens = min(self.max_per_second, self.tokens + elapsed * self.max_per_second)
            self.last_update = now
            
            if self.tokens < 1:
                wait_time = (1 - self.tokens) / self.max_per_second
                await asyncio.sleep(wait_time)
                self.tokens = 0
            else:
                self.tokens -= 1


class RPCClient:
    """Async RPC client for Solana with rate limiting and backoff."""
    
    def __init__(self, config: Config):
        self.config = config
        self.rpc_url = config.rpc_url
        self.rate_limiter = RateLimiter(MAX_REQUESTS_PER_SECOND)
        self._session: Optional[aiohttp.ClientSession] = None
        self._backoff_until: float = 0
        self._consecutive_errors = 0
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    
    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def _wait_for_backoff(self) -> None:
        """Wait if we're in backoff period."""
        now = time.monotonic()
        if now < self._backoff_until:
            wait_time = self._backoff_until - now
            logger.warning("rpc_backoff_waiting", wait_seconds=wait_time)
            await asyncio.sleep(wait_time)
    
    def _apply_backoff(self) -> None:
        """Apply exponential backoff after an error."""
        self._consecutive_errors += 1
        backoff = min(
            BACKOFF_BASE_SECONDS * (2 ** self._consecutive_errors),
            BACKOFF_MAX_SECONDS
        )
        self._backoff_until = time.monotonic() + backoff
        logger.warning("rpc_backoff_applied", backoff_seconds=backoff)
    
    def _reset_backoff(self) -> None:
        """Reset backoff after successful request."""
        self._consecutive_errors = 0
        self._backoff_until = 0
    
    async def _request(self, method: str, params: List[Any]) -> Dict[str, Any]:
        """Make a JSON-RPC request."""
        await self._wait_for_backoff()
        await self.rate_limiter.acquire()
        
        session = await self._get_session()
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params
        }
        
        try:
            async with session.post(
                self.rpc_url,
                json=payload,
                headers={"Content-Type": "application/json"}
            ) as response:
                if response.status == 429:
                    self._apply_backoff()
                    raise Exception("Rate limited by RPC")
                
                response.raise_for_status()
                result = await response.json()
                
                if "error" in result:
                    error = result["error"]
                    raise Exception(f"RPC error: {error.get('message', error)}")
                
                self._reset_backoff()
                return result.get("result", {})
                
        except aiohttp.ClientError as e:
            self._apply_backoff()
            logger.error("rpc_request_failed", method=method, error=str(e))
            raise
    
    async def get_balance(self, pubkey: Pubkey) -> int:
        """Get SOL balance in lamports."""
        result = await self._request("getBalance", [str(pubkey)])
        return result.get("value", 0)
    
    async def get_latest_blockhash(self) -> str:
        """Get the latest blockhash."""
        result = await self._request("getLatestBlockhash", [{"commitment": "finalized"}])
        return result["value"]["blockhash"]
    
    async def get_token_account_balance(self, token_account: str) -> Dict[str, Any]:
        """Get token account balance."""
        result = await self._request("getTokenAccountBalance", [token_account])
        return result.get("value", {})
    
    async def send_transaction(
        self, 
        transaction: VersionedTransaction,
        skip_preflight: bool = False
    ) -> str:
        """Send a signed transaction and return signature."""
        # Serialize transaction to base64
        tx_bytes = bytes(transaction)
        import base64
        tx_base64 = base64.b64encode(tx_bytes).decode('utf-8')
        
        options = {
            "skipPreflight": skip_preflight,
            "preflightCommitment": "confirmed",
            "encoding": "base64"
        }
        
        result = await self._request("sendTransaction", [tx_base64, options])
        
        if isinstance(result, str):
            return result
        
        raise Exception(f"Unexpected sendTransaction result: {result}")
    
    async def confirm_transaction(
        self, 
        signature: str, 
        timeout_seconds: float = 30.0
    ) -> bool:
        """Wait for transaction confirmation."""
        start_time = time.monotonic()
        
        while time.monotonic() - start_time < timeout_seconds:
            try:
                result = await self._request(
                    "getSignatureStatuses", 
                    [[signature], {"searchTransactionHistory": True}]
                )
                
                statuses = result.get("value", [])
                if statuses and statuses[0]:
                    status = statuses[0]
                    if status.get("err"):
                        logger.error(
                            "transaction_failed",
                            signature=signature,
                            error=status["err"]
                        )
                        return False
                    
                    confirmation_status = status.get("confirmationStatus")
                    if confirmation_status in ("confirmed", "finalized"):
                        logger.info(
                            "transaction_confirmed",
                            signature=signature,
                            status=confirmation_status
                        )
                        return True
                
                await asyncio.sleep(0.5)
                
            except Exception as e:
                logger.warning(
                    "confirm_transaction_error",
                    signature=signature,
                    error=str(e)
                )
                await asyncio.sleep(1.0)
        
        logger.warning("transaction_timeout", signature=signature)
        return False
    
    async def get_sol_price_usd(self) -> float:
        """Get approximate SOL price in USD using on-chain oracle or API."""
        # For simplicity, we'll use a default estimate
        # In production, integrate with Pyth or Switchboard oracle
        return 100.0  # Default estimate, should be updated with real price
    
    async def simulate_transaction(self, transaction: VersionedTransaction) -> Dict[str, Any]:
        """Simulate a transaction before sending."""
        import base64
        tx_bytes = bytes(transaction)
        tx_base64 = base64.b64encode(tx_bytes).decode('utf-8')
        
        result = await self._request(
            "simulateTransaction",
            [tx_base64, {"encoding": "base64", "commitment": "confirmed"}]
        )
        
        return result.get("value", {})
    
    async def get_signatures_for_address(self, pubkey: Pubkey, limit: int = 20) -> List[Dict]:
        """Get recent transaction signatures for an address."""
        result = await self._request(
            "getSignaturesForAddress",
            [str(pubkey), {"limit": limit}]
        )
        return result if isinstance(result, list) else []
    
    async def get_transaction(self, signature: str) -> Optional[Dict]:
        """Get a transaction by signature."""
        try:
            result = await self._request(
                "getTransaction",
                [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
            )
            return result
        except Exception as e:
            logger.debug("get_transaction_error", signature=signature[:16], error=str(e))
            return None


def create_rpc_client(config: Config) -> RPCClient:
    """Factory function to create an RPC client."""
    return RPCClient(config)
