"""
Transaction Parser - Analyzes Solana transactions to detect swaps.
Identifies buys/sells on Pump.fun, Jupiter, Raydium, etc.
"""

from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from enum import Enum
import structlog

logger = structlog.get_logger(__name__)

# Known program IDs
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
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
            programs_involved = [k for k in account_keys if k in [PUMP_FUN_PROGRAM, JUPITER_V6_PROGRAM, RAYDIUM_AMM_PROGRAM, RAYDIUM_CLMM_PROGRAM]]
            logger.debug("tx_programs", wallet=wallet[:8], programs=len(programs_involved), has_pump=PUMP_FUN_PROGRAM in account_keys)
            
            # Get instructions
            instructions = message.get("instructions", [])
            inner_instructions = meta.get("innerInstructions", [])
            
            # Try to detect swap from different DEXes
            swap = None
            
            # Check for Pump.fun swap
            swap = self._parse_pump_fun(tx_data, wallet, account_keys)
            if swap:
                return swap
            
            # Check for Jupiter swap
            swap = self._parse_jupiter(tx_data, wallet, account_keys, meta)
            if swap:
                return swap
            
            # Check for Raydium swap
            swap = self._parse_raydium(tx_data, wallet, account_keys, meta)
            if swap:
                return swap
            
            # Fallback: detect from balance changes
            swap = self._parse_from_balance_changes(tx_data, wallet, meta)
            if swap:
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
        """Parse Pump.fun swap."""
        # Check if Pump.fun program is involved
        if PUMP_FUN_PROGRAM not in account_keys:
            logger.debug("pump_fun_not_in_keys", wallet=wallet[:8])
            return None
        
        logger.debug("pump_fun_program_found", wallet=wallet[:8])
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
        
        # Determine if buy or sell
        if token_change > 0 and sol_change < 0:
            swap_type = SwapType.BUY
        elif token_change < 0 and sol_change > 0:
            swap_type = SwapType.SELL
        else:
            return None
        
        # Filter by minimum SOL value
        if abs(sol_change) / 1e9 < self.min_sol_value:
            return None
        
        return ParsedSwap(
            swap_type=swap_type,
            token_mint=token_mint,
            token_symbol=None,  # Would need to fetch from metadata
            sol_amount=abs(sol_change),
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
    
    def _parse_from_balance_changes(
        self, 
        tx_data: Dict, 
        wallet: str, 
        meta: Dict,
        dex: str = "unknown"
    ) -> Optional[ParsedSwap]:
        """
        Fallback parser that detects swaps from balance changes.
        Works for any DEX.
        """
        # Get pre and post token balances for this wallet
        pre_balances = {}
        post_balances = {}
        
        for b in meta.get("preTokenBalances", []):
            if b.get("owner") == wallet:
                pre_balances[b.get("mint")] = int(b.get("uiTokenAmount", {}).get("amount", "0"))
        
        for b in meta.get("postTokenBalances", []):
            if b.get("owner") == wallet:
                post_balances[b.get("mint")] = int(b.get("uiTokenAmount", {}).get("amount", "0"))
        
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
        
        # Find the non-SOL/stable token that changed
        token_mint = None
        token_change = 0
        
        all_mints = set(pre_balances.keys()) | set(post_balances.keys())
        for mint in all_mints:
            if mint in (NATIVE_SOL_MINT, USDC_MINT, USDT_MINT):
                continue
            
            pre_amount = pre_balances.get(mint, 0)
            post_amount = post_balances.get(mint, 0)
            change = post_amount - pre_amount
            
            if abs(change) > 0:
                token_mint = mint
                token_change = change
                break
        
        if not token_mint:
            return None
        
        # Determine swap type
        if token_change > 0 and sol_change < 0:
            swap_type = SwapType.BUY
        elif token_change < 0 and sol_change > 0:
            swap_type = SwapType.SELL
        else:
            # Could be token-to-token swap, skip for now
            return None
        
        # Filter by minimum SOL value
        if abs(sol_change) / 1e9 < self.min_sol_value:
            return None
        
        signature = ""
        if "transaction" in tx_data:
            sigs = tx_data["transaction"].get("signatures", [])
            signature = sigs[0] if sigs else ""
        
        return ParsedSwap(
            swap_type=swap_type,
            token_mint=token_mint,
            token_symbol=None,
            sol_amount=abs(sol_change),
            token_amount=abs(token_change),
            dex=dex,
            signature=signature,
            wallet=wallet
        )
