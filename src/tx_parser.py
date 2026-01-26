"""
Transaction Parser - Analyzes Solana transactions to detect swaps.
Identifies buys/sells on Pump.fun, Jupiter, Raydium, etc.
"""

from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from enum import Enum
from datetime import datetime, timezone
import structlog

logger = structlog.get_logger(__name__)

# Known program IDs
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_FUN_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"  # For graduated tokens
JUPITER_V6_PROGRAM = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
RAYDIUM_AMM_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_CLMM_PROGRAM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"

# Native SOL mint
NATIVE_SOL_MINT = "So11111111111111111111111111111111111111112"

# Stablecoins
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"


class SwapType(Enum):
    BUY = "buy"      # SOL/Stable → Token
    SELL = "sell"    # Token → SOL/Stable
    UNKNOWN = "unknown"


@dataclass
class ParsedSwap:
    """Represents a parsed swap from a transaction."""
    swap_type: SwapType
    token_mint: str           # The token being bought/sold
    token_symbol: Optional[str]
    sol_amount: int           # Amount in lamports
    token_amount: int         # Amount in token base units
    dex: str                  # pump.fun, jupiter, raydium
    signature: str
    wallet: str
    block_time: Optional[datetime] = None
    slot: Optional[int] = None
    
    @property
    def sol_value(self) -> float:
        """SOL amount as float."""
        return self.sol_amount / 1e9
    
    @property
    def is_buy(self) -> bool:
        return self.swap_type == SwapType.BUY
    
    @property
    def is_sell(self) -> bool:
        return self.swap_type == SwapType.SELL


class TransactionParser:
    """
    Parses Solana transactions to extract swap information.
    """
    
    def __init__(self, min_sol_value: float = 0.01):
        """
        Initialize parser.
        
        Args:
            min_sol_value: Minimum SOL value to consider (filters dust)
        """
        self.min_sol_value = min_sol_value
    
    def parse_transaction(self, tx_data: Dict[str, Any], wallet: str) -> Optional[ParsedSwap]:
        """
        Parse a transaction and extract swap information.
        
        Args:
            tx_data: Raw transaction data from RPC
            wallet: The wallet address that made this transaction
        
        Returns:
            ParsedSwap if a swap was detected, None otherwise
        """
        try:
            # Check if transaction was successful
            meta = tx_data.get("meta", {})
            if meta.get("err") is not None:
                logger.debug("tx_failed", wallet=wallet[:8])
                return None
            
            # Get transaction message
            transaction = tx_data.get("transaction", {})
            message = transaction.get("message", {})
            
            # Get account keys
            account_keys = self._get_account_keys(message, meta)
            
            # Log programs involved
            known_programs = [PUMP_FUN_PROGRAM, PUMP_FUN_AMM_PROGRAM, JUPITER_V6_PROGRAM, RAYDIUM_AMM_PROGRAM, RAYDIUM_CLMM_PROGRAM]
            programs_involved = [k for k in account_keys if k in known_programs]
            has_pump = PUMP_FUN_PROGRAM in account_keys or PUMP_FUN_AMM_PROGRAM in account_keys
            
            # Log first 5 account keys to help identify unknown programs
            sample_keys = [k[:12] for k in account_keys[:8]] if account_keys else []
            logger.debug("tx_programs", wallet=wallet[:8], programs=len(programs_involved), has_pump=has_pump, has_pump_amm=PUMP_FUN_AMM_PROGRAM in account_keys, total_keys=len(account_keys), sample=",".join(sample_keys))
            
            # Get instructions
            instructions = message.get("instructions", [])
            inner_instructions = meta.get("innerInstructions", [])
            
            # Try to detect swap from different DEXes
            swap = None
            
            # Check for Pump.fun swap
            swap = self._parse_pump_fun(tx_data, wallet, account_keys)
            if swap:
                block_time_unix = tx_data.get("blockTime")
                if block_time_unix:
                    swap.block_time = datetime.fromtimestamp(block_time_unix, tz=timezone.utc)
                swap.slot = tx_data.get("slot")
                return swap
            
            # Check for Jupiter swap
            swap = self._parse_jupiter(tx_data, wallet, account_keys, meta)
            if swap:
                block_time_unix = tx_data.get("blockTime")
                if block_time_unix:
                    swap.block_time = datetime.fromtimestamp(block_time_unix, tz=timezone.utc)
                swap.slot = tx_data.get("slot")
                return swap
            
            # Check for Raydium swap
            swap = self._parse_raydium(tx_data, wallet, account_keys, meta)
            if swap:
                block_time_unix = tx_data.get("blockTime")
                if block_time_unix:
                    swap.block_time = datetime.fromtimestamp(block_time_unix, tz=timezone.utc)
                swap.slot = tx_data.get("slot")
                return swap
            
            # Fallback: detect from balance changes
            swap = self._parse_from_balance_changes(tx_data, wallet, meta)
            if swap:
                block_time_unix = tx_data.get("blockTime")
                if block_time_unix:
                    swap.block_time = datetime.fromtimestamp(block_time_unix, tz=timezone.utc)
                swap.slot = tx_data.get("slot")
                return swap
            
            return None
            
        except Exception as e:
            logger.warning("parse_error", error=str(e))
            return None
    
    def _get_account_keys(self, message: Dict, meta: Dict) -> List[str]:
        """Extract all account keys from transaction."""
        keys = []
        
        # Static account keys
        account_keys = message.get("accountKeys", [])
        for key in account_keys:
            if isinstance(key, str):
                keys.append(key)
            elif isinstance(key, dict):
                keys.append(key.get("pubkey", ""))
        
        # Loaded addresses (for versioned transactions)
        loaded = meta.get("loadedAddresses", {})
        keys.extend(loaded.get("writable", []))
        keys.extend(loaded.get("readonly", []))
        
        return keys
    
    def _parse_pump_fun(
        self, 
        tx_data: Dict, 
        wallet: str, 
        account_keys: List[str]
    ) -> Optional[ParsedSwap]:
        """Parse Pump.fun swap (bonding curve or AMM)."""
        # Check if any Pump.fun program is involved
        has_bonding_curve = PUMP_FUN_PROGRAM in account_keys
        has_amm = PUMP_FUN_AMM_PROGRAM in account_keys
        
        if not has_bonding_curve and not has_amm:
            logger.debug("pump_fun_not_in_keys", wallet=wallet[:8])
            return None
        
        logger.debug("pump_fun_program_found", wallet=wallet[:8], bonding_curve=has_bonding_curve, amm=has_amm)
        meta = tx_data.get("meta", {})
        
        # Get SOL balance change for the wallet (first signer)
        account_keys_list = self._get_account_keys(
            tx_data.get("transaction", {}).get("message", {}), 
            meta
        )
        
        # For pump.fun, the fee payer (index 0) is usually the trader
        # Try to find wallet in account keys, fallback to index 0
        wallet_index = -1
        if wallet in account_keys_list:
            wallet_index = account_keys_list.index(wallet)
        elif len(account_keys_list) > 0:
            # Wallet might be interacting via different account, use first signer
            wallet_index = 0
        
        sol_change = 0
        if wallet_index >= 0:
            pre_sol = meta.get("preBalances", [])[wallet_index] if wallet_index < len(meta.get("preBalances", [])) else 0
            post_sol = meta.get("postBalances", [])[wallet_index] if wallet_index < len(meta.get("postBalances", [])) else 0
            sol_change = post_sol - pre_sol
        
        # Also check wrapped SOL changes for this wallet
        wsol_change = self._get_wrapped_sol_change(meta, wallet)
        
        # For pump.fun, look at ALL token balance changes (not just wallet-owned)
        # Since this is a pump.fun tx initiated by the wallet, token changes are theirs
        pre_balances_all = {}
        post_balances_all = {}
        
        for b in meta.get("preTokenBalances", []):
            mint = b.get("mint")
            if mint and mint not in (NATIVE_SOL_MINT, USDC_MINT, USDT_MINT):
                pre_balances_all[mint] = int(b.get("uiTokenAmount", {}).get("amount", "0"))
        
        for b in meta.get("postTokenBalances", []):
            mint = b.get("mint")
            if mint and mint not in (NATIVE_SOL_MINT, USDC_MINT, USDT_MINT):
                post_balances_all[mint] = int(b.get("uiTokenAmount", {}).get("amount", "0"))
        
        # Find token that changed
        token_mint = None
        token_change = 0
        
        all_mints = set(pre_balances_all.keys()) | set(post_balances_all.keys())
        logger.debug("pump_fun_balances", 
            wallet=wallet[:8],
            sol_change=sol_change,
            wsol_change=wsol_change,
            pre_mints=len(pre_balances_all),
            post_mints=len(post_balances_all),
            all_mints=len(all_mints)
        )
        
        for mint in all_mints:
            pre_amount = pre_balances_all.get(mint, 0)
            post_amount = post_balances_all.get(mint, 0)
            change = post_amount - pre_amount
            
            if change != 0:
                token_mint = mint
                token_change = change
                logger.debug("pump_fun_token_change", token=mint[:8], change=change)
                break
        
        if not token_mint:
            logger.debug("pump_fun_no_token_change", wallet=wallet[:8])
            return None
        
        # Combine native SOL and wrapped SOL changes for total SOL movement
        # Use the larger absolute value, or add them if they're in same direction
        total_sol_change = sol_change
        if wsol_change != 0:
            # If wsol_change is significant and sol_change is near 0 (just fees), use wsol
            if abs(sol_change) < 0.01 * 1e9:  # Less than 0.01 SOL (probably just fees)
                total_sol_change = wsol_change
            elif (sol_change > 0 and wsol_change > 0) or (sol_change < 0 and wsol_change < 0):
                # Same direction, add them
                total_sol_change = sol_change + wsol_change
            else:
                # Different directions, use the larger magnitude
                total_sol_change = sol_change if abs(sol_change) > abs(wsol_change) else wsol_change
        
        # Determine if buy or sell - now with improved logic
        swap_type = None
        estimated_sol = abs(total_sol_change)
        
        if token_change > 0 and total_sol_change < 0:
            # Clear buy: gained tokens, lost SOL
            swap_type = SwapType.BUY
        elif token_change < 0 and total_sol_change > 0:
            # Clear sell: lost tokens, gained SOL
            swap_type = SwapType.SELL
        elif token_change > 0 and total_sol_change >= 0:
            # Gained tokens but SOL change unclear - likely a BUY where fees offset
            # Estimate SOL value from token change and typical pump.fun mechanics
            swap_type = SwapType.BUY
            # Estimate: if we can't determine SOL, use a reasonable estimate
            # This is better than missing the trade entirely
            if estimated_sol < self.min_sol_value * 1e9:
                estimated_sol = self._estimate_sol_from_pump_fun(tx_data, token_change)
            logger.debug("pump_fun_inferred_buy", token=token_mint[:8], estimated_sol=estimated_sol/1e9)
        elif token_change < 0 and total_sol_change <= 0:
            # Lost tokens but SOL change unclear - likely a SELL where we received wrapped SOL
            swap_type = SwapType.SELL
            if estimated_sol < self.min_sol_value * 1e9:
                estimated_sol = self._estimate_sol_from_pump_fun(tx_data, token_change)
            logger.debug("pump_fun_inferred_sell", token=token_mint[:8], estimated_sol=estimated_sol/1e9)
        
        if not swap_type:
            logger.debug("pump_fun_cannot_determine_type", 
                token=token_mint[:8], 
                token_change=token_change, 
                sol_change=total_sol_change)
            return None
        
        # Filter by minimum SOL value
        if estimated_sol / 1e9 < self.min_sol_value:
            logger.debug("pump_fun_below_min_sol", estimated_sol=estimated_sol/1e9)
            return None
        
        return ParsedSwap(
            swap_type=swap_type,
            token_mint=token_mint,
            token_symbol=None,  # Would need to fetch from metadata
            sol_amount=int(estimated_sol),
            token_amount=abs(token_change),
            dex="pump.fun",
            signature=tx_data.get("transaction", {}).get("signatures", [""])[0],
            wallet=wallet
        )
    
    def _parse_jupiter(
        self, 
        tx_data: Dict, 
        wallet: str, 
        account_keys: List[str],
        meta: Dict
    ) -> Optional[ParsedSwap]:
        """Parse Jupiter swap."""
        if JUPITER_V6_PROGRAM not in account_keys:
            return None
        
        # Use same balance-change logic as pump.fun
        return self._parse_from_balance_changes(tx_data, wallet, meta, dex="jupiter")
    
    def _parse_raydium(
        self, 
        tx_data: Dict, 
        wallet: str, 
        account_keys: List[str],
        meta: Dict
    ) -> Optional[ParsedSwap]:
        """Parse Raydium swap."""
        if RAYDIUM_AMM_PROGRAM not in account_keys and RAYDIUM_CLMM_PROGRAM not in account_keys:
            return None
        
        return self._parse_from_balance_changes(tx_data, wallet, meta, dex="raydium")
    
    def _get_wrapped_sol_change(self, meta: Dict, wallet: str) -> int:
        """
        Get wrapped SOL (WSOL) balance change for a wallet.
        Many DEXes use wrapped SOL for swaps.
        """
        wsol_pre = 0
        wsol_post = 0
        
        # Check all token balances for wrapped SOL owned by this wallet
        for b in meta.get("preTokenBalances", []):
            if b.get("mint") == NATIVE_SOL_MINT and b.get("owner") == wallet:
                wsol_pre = int(b.get("uiTokenAmount", {}).get("amount", "0"))
        
        for b in meta.get("postTokenBalances", []):
            if b.get("mint") == NATIVE_SOL_MINT and b.get("owner") == wallet:
                wsol_post = int(b.get("uiTokenAmount", {}).get("amount", "0"))
        
        return wsol_post - wsol_pre
    
    def _estimate_sol_from_pump_fun(self, tx_data: Dict, token_change: int) -> int:
        """
        Estimate SOL value when we can't determine it directly.
        Uses total SOL movement in transaction as a proxy.
        """
        meta = tx_data.get("meta", {})
        pre_balances = meta.get("preBalances", [])
        post_balances = meta.get("postBalances", [])
        
        # Calculate total SOL movement (excluding fees)
        # Look for significant SOL movements between accounts
        max_sol_movement = 0
        
        for i in range(min(len(pre_balances), len(post_balances))):
            change = abs(post_balances[i] - pre_balances[i])
            # Skip very small changes (likely fees) and very large ones (likely pool reserves)
            if change > 0.01 * 1e9 and change < 1000 * 1e9:
                if change > max_sol_movement:
                    max_sol_movement = change
        
        # If we found a reasonable movement, use it
        if max_sol_movement > self.min_sol_value * 1e9:
            return int(max_sol_movement)
        
        # Fallback: estimate based on typical pump.fun trade sizes
        # Use 0.1 SOL as a conservative estimate
        return int(0.1 * 1e9)
    
    def _parse_from_balance_changes(
        self, 
        tx_data: Dict, 
        wallet: str, 
        meta: Dict,
        dex: str = "unknown"
    ) -> Optional[ParsedSwap]:
        """
        Fallback parser that detects swaps from balance changes.
        Works for any DEX. Now with improved detection for edge cases.
        """
        # Get pre and post token balances - check ALL accounts, not just wallet-owned
        # because some DEXes route through intermediate accounts
        pre_balances = {}
        post_balances = {}
        wallet_owned_pre = {}
        wallet_owned_post = {}
        
        for b in meta.get("preTokenBalances", []):
            mint = b.get("mint")
            amount = int(b.get("uiTokenAmount", {}).get("amount", "0"))
            if b.get("owner") == wallet:
                wallet_owned_pre[mint] = amount
            # Track all balances
            if mint not in pre_balances:
                pre_balances[mint] = 0
            pre_balances[mint] += amount
        
        for b in meta.get("postTokenBalances", []):
            mint = b.get("mint")
            amount = int(b.get("uiTokenAmount", {}).get("amount", "0"))
            if b.get("owner") == wallet:
                wallet_owned_post[mint] = amount
            # Track all balances
            if mint not in post_balances:
                post_balances[mint] = 0
            post_balances[mint] += amount
        
        # Get SOL balance change
        account_keys = self._get_account_keys(
            tx_data.get("transaction", {}).get("message", {}),
            meta
        )
        wallet_index = account_keys.index(wallet) if wallet in account_keys else -1
        
        sol_change = 0
        if wallet_index >= 0 and wallet_index < len(meta.get("preBalances", [])):
            pre_sol = meta.get("preBalances", [])[wallet_index]
            post_sol = meta.get("postBalances", [])[wallet_index]
            sol_change = post_sol - pre_sol
        
        # Also check wrapped SOL
        wsol_change = self._get_wrapped_sol_change(meta, wallet)
        total_sol_change = sol_change
        if wsol_change != 0:
            if abs(sol_change) < 0.01 * 1e9:
                total_sol_change = wsol_change
            elif (sol_change > 0 and wsol_change > 0) or (sol_change < 0 and wsol_change < 0):
                total_sol_change = sol_change + wsol_change
            else:
                total_sol_change = sol_change if abs(sol_change) > abs(wsol_change) else wsol_change
        
        # Find the non-SOL/stable token that changed for THIS wallet
        # Prefer wallet-owned changes, fall back to all changes
        token_mint = None
        token_change = 0
        
        # First try wallet-owned balances
        all_wallet_mints = set(wallet_owned_pre.keys()) | set(wallet_owned_post.keys())
        for mint in all_wallet_mints:
            if mint in (NATIVE_SOL_MINT, USDC_MINT, USDT_MINT):
                continue
            
            pre_amount = wallet_owned_pre.get(mint, 0)
            post_amount = wallet_owned_post.get(mint, 0)
            change = post_amount - pre_amount
            
            if abs(change) > 0:
                token_mint = mint
                token_change = change
                break
        
        # If no wallet-owned token changed, check all token changes
        # (for cases where ownership isn't properly tagged)
        if not token_mint:
            all_mints = set(pre_balances.keys()) | set(post_balances.keys())
            for mint in all_mints:
                if mint in (NATIVE_SOL_MINT, USDC_MINT, USDT_MINT):
                    continue
                
                pre_amount = pre_balances.get(mint, 0)
                post_amount = post_balances.get(mint, 0)
                change = post_amount - pre_amount
                
                # For non-wallet-owned, we look for significant changes that 
                # correlate with SOL movement (indicating a swap)
                if abs(change) > 0 and abs(total_sol_change) > 0.01 * 1e9:
                    token_mint = mint
                    token_change = change
                    break
        
        if not token_mint:
            return None
        
        # Determine swap type with improved logic
        swap_type = None
        estimated_sol = abs(total_sol_change)
        
        if token_change > 0 and total_sol_change < 0:
            swap_type = SwapType.BUY
        elif token_change < 0 and total_sol_change > 0:
            swap_type = SwapType.SELL
        elif token_change > 0 and total_sol_change >= 0:
            # Infer buy from token gain
            swap_type = SwapType.BUY
            if estimated_sol < self.min_sol_value * 1e9:
                estimated_sol = self._estimate_sol_from_pump_fun(tx_data, token_change)
        elif token_change < 0 and total_sol_change <= 0:
            # Infer sell from token loss
            swap_type = SwapType.SELL
            if estimated_sol < self.min_sol_value * 1e9:
                estimated_sol = self._estimate_sol_from_pump_fun(tx_data, token_change)
        
        if not swap_type:
            return None
        
        # Filter by minimum SOL value
        if estimated_sol / 1e9 < self.min_sol_value:
            return None
        
        signature = ""
        if "transaction" in tx_data:
            sigs = tx_data["transaction"].get("signatures", [])
            signature = sigs[0] if sigs else ""
        
        return ParsedSwap(
            swap_type=swap_type,
            token_mint=token_mint,
            token_symbol=None,
            sol_amount=int(estimated_sol),
            token_amount=abs(token_change),
            dex=dex,
            signature=signature,
            wallet=wallet
        )
