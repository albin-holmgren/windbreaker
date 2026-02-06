"""
Vercel AI Gateway Parser - Fast token extraction from messages.
Uses Vercel AI Gateway for OpenAI/Anthropic models with low latency.
"""

import asyncio
import aiohttp
from typing import Optional
from datetime import datetime
import structlog

logger = structlog.get_logger(__name__)

# Vercel AI Gateway endpoint
VERCEL_AI_GATEWAY_URL = "https://ai-gateway.vercel.com/v1/chat/completions"

# System prompt for token extraction
TOKEN_EXTRACTION_PROMPT = """You are a crypto trading signal parser. Extract Solana token addresses from Telegram messages.

Rules:
1. Look for base58-encoded Solana addresses (32-44 characters, alphanumeric)
2. Return ONLY the token address, nothing else
3. If no valid address found, return "none"
4. Ignore wallet addresses (typically 43-44 chars), focus on token/mint addresses (32-44 chars)
5. If multiple addresses, return the one most likely to be a token (usually shorter)

Example outputs:
- Input: "Buy $PEPE now! CA: DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
- Output: DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263

- Input: "Great project!"
- Output: none

Extract the address from this message:"""


class AIGatewayParser:
    """Parse Telegram messages using Vercel AI Gateway."""
    
    def __init__(
        self,
        api_key: str,
        model: str = "openai/gpt-4o-mini",  # Fast and cheap
        timeout_ms: int = 500,
        confidence_threshold: float = 0.0,  # Trust everything
    ):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout_ms / 1000.0  # Convert to seconds
        self.confidence_threshold = confidence_threshold
        
        self.session: Optional[aiohttp.ClientSession] = None
        
        # Stats
        self.requests_made = 0
        self.timeouts = 0
        self.tokens_found = 0
        
    async def start(self) -> None:
        """Initialize HTTP session."""
        self.session = aiohttp.ClientSession()
        logger.info("ai_parser_started", model=self.model, timeout_ms=int(self.timeout * 1000))
    
    async def stop(self) -> None:
        """Close HTTP session."""
        if self.session:
            await self.session.close()
    
    async def extract_token(self, message_text: str) -> Optional[str]:
        """
        Extract token address from message using AI.
        Returns None if no address found or timeout.
        """
        if not self.session:
            await self.start()
        
        self.requests_made += 1
        
        try:
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": TOKEN_EXTRACTION_PROMPT},
                    {"role": "user", "content": message_text}
                ],
                "max_tokens": 100,
                "temperature": 0.0,
                "stream": False
            }
            
            async with self.session.post(
                VERCEL_AI_GATEWAY_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.warning("ai_gateway_error", status=resp.status, error=error_text[:100])
                    return None
                
                data = await resp.json()
                
                if not data or "choices" not in data or not data["choices"]:
                    logger.warning("ai_invalid_response", data=str(data)[:200])
                    return None
                
                content = data["choices"][0].get("message", {}).get("content", "").strip()
                
                # Check if it's a valid address (not "none")
                if content.lower() == "none" or not content:
                    return None
                
                # Validate it looks like a Solana address
                if len(content) < 32 or len(content) > 44:
                    logger.debug("ai_returned_invalid_length", content=content[:50])
                    return None
                
                self.tokens_found += 1
                
                logger.info("token_extracted", 
                           token=content[:8] + "...",
                           model=self.model,
                           latency_ms=int(self.timeout * 1000))
                
                return content
                
        except asyncio.TimeoutError:
            self.timeouts += 1
            logger.debug("ai_timeout", timeouts=self.timeouts)
            return None
        except Exception as e:
            logger.error("ai_extraction_error", error=str(e))
            return None
    
    async def extract_token_fast(self, message_text: str) -> Optional[str]:
        """
        Fast extraction with fallback to regex if AI fails.
        First tries AI, falls back to regex pattern matching if timeout/error.
        """
        # Try AI first
        result = await self.extract_token(message_text)
        if result:
            return result
        
        # Fallback: regex extraction
        import re
        pattern = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')
        matches = pattern.findall(message_text)
        
        if matches:
            # Return first match that's likely a token (not a wallet)
            for match in matches:
                # Token addresses are typically 32-36 chars
                # Wallet addresses are typically 43-44 chars
                if 32 <= len(match) <= 40:
                    logger.info("token_extracted_fallback", token=match[:8] + "...")
                    return match
            # If no short ones, return the first
            return matches[0]
        
        return None
    
    def get_stats(self) -> dict:
        """Get parser stats."""
        return {
            "requests_made": self.requests_made,
            "tokens_found": self.tokens_found,
            "timeouts": self.timeouts,
            "success_rate": f"{(self.tokens_found / max(self.requests_made, 1) * 100):.1f}%"
        }
