"""
Wallet abstraction for Windbreaker.
Handles loading keys and signing transactions.
"""

import base58
from typing import Optional
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction, VersionedTransaction
from solders.message import Message
import structlog

from .config import Config

logger = structlog.get_logger()


class Wallet:
    """Wallet abstraction for signing Solana transactions."""
    
    def __init__(self, config: Config):
        """Initialize wallet from configuration."""
        self.config = config
        self._keypair: Optional[Keypair] = None
        self._load_keypair()
    
    def _load_keypair(self) -> None:
        """Load keypair from private key in config."""
        try:
            # Decode base58 private key
            private_key_bytes = base58.b58decode(self.config.wallet_private_key)
            
            # Solana keypairs are 64 bytes (32 byte private + 32 byte public)
            if len(private_key_bytes) == 64:
                self._keypair = Keypair.from_bytes(private_key_bytes)
            elif len(private_key_bytes) == 32:
                # Just the private key seed
                self._keypair = Keypair.from_seed(private_key_bytes)
            else:
                raise ValueError(f"Invalid private key length: {len(private_key_bytes)}")
            
            logger.info(
                "wallet_loaded",
                address=str(self.pubkey),
                network=self.config.network
            )
        except Exception as e:
            logger.error("wallet_load_failed", error=str(e))
            raise ValueError(f"Failed to load wallet: {e}")
    
    @property
    def keypair(self) -> Keypair:
        """Get the keypair."""
        if self._keypair is None:
            raise ValueError("Wallet not initialized")
        return self._keypair
    
    @property
    def pubkey(self) -> Pubkey:
        """Get the public key."""
        return self.keypair.pubkey()
    
    @property
    def address(self) -> str:
        """Get the wallet address as string."""
        return str(self.pubkey)
    
    def sign_transaction(self, transaction: Transaction) -> Transaction:
        """Sign a legacy transaction."""
        transaction.sign([self.keypair], transaction.message.recent_blockhash)
        return transaction
    
    def sign_versioned_transaction(self, transaction: VersionedTransaction) -> VersionedTransaction:
        """Sign a versioned transaction."""
        # For versioned transactions, we need to create a new signed transaction
        signed_tx = VersionedTransaction(
            transaction.message,
            [self.keypair]
        )
        return signed_tx
    
    def sign_message(self, message: bytes) -> bytes:
        """Sign an arbitrary message."""
        return self.keypair.sign_message(message).to_bytes()


def create_wallet(config: Config) -> Wallet:
    """Factory function to create a wallet instance."""
    return Wallet(config)
