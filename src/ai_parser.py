"""
AI Gateway Parser - Fast token extraction from messages.
Uses OpenRouter (or any OpenAI-compatible API) for AI-powered token extraction.
Falls back to regex if AI fails.
"""

import asyncio
import aiohttp
from typing import Optional
from datetime import datetime
import structlog

logger = structlog.get_logger(__name__)

# Default AI endpoint (Vercel AI Gateway)
DEFAULT_AI_GATEWAY_URL = "https://ai-gateway.vercel.com/v1/chat/completions"

# System prompt for token extraction with fresh launch detection
TOKEN_EXTRACTION_PROMPT = """You are a crypto trading signal parser. Extract Solana token addresses from Telegram messages and classify if it's a FRESH LAUNCH or OLD CALL.

Rules:
1. Look for base58-encoded Solana addresses (32-44 characters, alphanumeric)
2. Analyze the message text to determine if this is a NEW/FRESH launch or an OLD call
3. Return format: ADDRESS|CLASSIFICATION
   - ADDRESS: the token address (or "none" if not found)
   - CLASSIFICATION: "fresh" for new launches, "old" for established coins, "unknown" if unclear

Fresh launch indicators: "NEW", "LAUNCH", "JUST", "FRESH", "MINT", "RELEASED", "NOW", "🟢", "🚀"
Old call indicators: "CALL", "GEM", "BUY", "HOLD", "SUPPORT", "ACCUMULATE", "DIP", "🎯"

Examples:
- Input: "🟢 NEW LAUNCH! CA: DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
- Output: DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263|fresh

- Input: "BUY $PEPE now! CA: DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
- Output: DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263|old

- Input: "Great project!"
- Output: none|unknown

Extract from this message:"""


class AIGatewayParser:
    """Parse Telegram messages using OpenRouter or any OpenAI-compatible API."""
    
    def __init__(
        self,
        api_key: str,
        model: str = "moonshotai/kimi-k2.5",
        timeout_ms: int = 3000,
        confidence_threshold: float = 0.0,  # Trust everything
        gateway_url: str = "",
    ):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout_ms / 1000.0  # Convert to seconds
        self.confidence_threshold = confidence_threshold
        self.gateway_url = gateway_url or DEFAULT_AI_GATEWAY_URL
        
        self.session: Optional[aiohttp.ClientSession] = None
        
        # Stats
        self.requests_made = 0
        self.timeouts = 0
        self.tokens_found = 0
        self.ai_errors = 0
        
    async def start(self) -> None:
        """Initialize HTTP session."""
        self.session = aiohttp.ClientSession()
        logger.info("ai_parser_started", 
                   model=self.model, 
                   timeout_ms=int(self.timeout * 1000),
                   gateway=self.gateway_url[:40])
    
    async def stop(self) -> None:
        """Close HTTP session."""
        if self.session:
            await self.session.close()
    
    async def extract_token(self, message_text: str) -> tuple[Optional[str], str]:
        """
        Extract token address from message using AI and classify as fresh/old.
        Returns (address, classification) where classification is "fresh", "old", or "unknown".
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
                self.gateway_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/albin-holmgren/windbreaker",
                    "X-Title": "Windbreaker Trading Bot"
                },
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    self.ai_errors += 1
                    if self.ai_errors <= 3 or self.ai_errors % 50 == 0:
                        logger.warning("ai_gateway_error", 
                                      status=resp.status, 
                                      error=error_text[:100],
                                      total_errors=self.ai_errors)
                    return None, "unknown"
                
                data = await resp.json()
                
                if not data or "choices" not in data or not data["choices"]:
                    logger.warning("ai_invalid_response", data=str(data)[:200])
                    return None, "unknown"
                
                content = data["choices"][0].get("message", {}).get("content", "").strip()
                
                # Parse the ADDRESS|CLASSIFICATION format
                parts = content.split("|")
                address_part = parts[0].strip() if parts else "none"
                classification = parts[1].strip().lower() if len(parts) > 1 else "unknown"
                
                # Check if it's a valid address (not "none")
                if address_part.lower() == "none" or not address_part:
                    return None, classification
                
                # Validate it looks like a Solana address
                if len(address_part) < 32 or len(address_part) > 44:
                    logger.debug("ai_returned_invalid_length", content=address_part[:50])
                    return None, classification
                
                self.tokens_found += 1
                
                logger.info("token_extracted", 
                           token=address_part[:8] + "...",
                           classification=classification,
                           model=self.model)
                
                return address_part, classification
                
        except asyncio.TimeoutError:
            self.timeouts += 1
            logger.debug("ai_timeout", timeouts=self.timeouts)
            return None, "unknown"
        except Exception as e:
            logger.error("ai_extraction_error", error=str(e))
            return None, "unknown"
    
    async def extract_token_fast(self, message_text: str) -> tuple[Optional[str], str]:
        """
        Fast extraction with fallback to regex if AI fails.
        First tries AI, falls back to regex pattern matching if timeout/error.
        Returns (address, classification) where classification is "fresh", "old", or "unknown".
        """
        # Try AI first
        address, classification = await self.extract_token(message_text)
        if address:
            return address, classification
        
        # Fallback: regex extraction - classify as unknown since we can't analyze message
        import re
        pattern = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')
        matches = pattern.findall(message_text)
        
        if matches:
            # Return first match that's likely a token (not a wallet)
            for match in matches:
                # Token addresses are typically 32-36 chars
                # Wallet addresses are typically 43-44 chars
                if 32 <= len(match) <= 40:
                    logger.info("token_extracted_fallback", token=match[:8] + "...", classification="unknown")
                    return match, "unknown"
            # If no short ones, return the first
            return matches[0], "unknown"
        
        return None, "unknown"
    
    def get_stats(self) -> dict:
        """Get parser stats."""
        return {
            "requests_made": self.requests_made,
            "tokens_found": self.tokens_found,
            "timeouts": self.timeouts,
            "ai_errors": self.ai_errors,
            "success_rate": f"{(self.tokens_found / max(self.requests_made, 1) * 100):.1f}%",
            "gateway": self.gateway_url[:40],
            "model": self.model
        }
