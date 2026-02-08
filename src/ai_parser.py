"""
AI Gateway Parser - Fast token extraction from messages with chat memory.
Uses OpenRouter (or any OpenAI-compatible API) for AI-powered token extraction.
Falls back to regex if AI fails.
Tracks chat history to understand launch timing and context.
"""

import asyncio
import aiohttp
from typing import Optional
from datetime import datetime, timedelta
from collections import deque
import structlog

logger = structlog.get_logger(__name__)

# Default AI endpoint (Vercel AI Gateway)
DEFAULT_AI_GATEWAY_URL = "https://ai-gateway.vercel.com/v1/chat/completions"

# System prompt for token extraction with fresh launch detection and context
TOKEN_EXTRACTION_PROMPT = """You are a crypto trading signal parser. Extract Solana token addresses from Telegram messages and classify if it's a FRESH LAUNCH or OLD CALL.

CONTEXT: You have access to recent chat history including any "launch in X hours" announcements.

Rules:
1. Look for base58-encoded Solana addresses (32-44 characters, alphanumeric)
2. Analyze the message text to determine if this is a NEW/FRESH launch or an OLD call
3. Consider timing: If there was a recent "launch in X hours" announcement, and this CA appears around that time, it's likely the real launch
4. Return format: ADDRESS|CLASSIFICATION|CONFIDENCE
   - ADDRESS: the token address (or "none" if not found)
   - CLASSIFICATION: "fresh" for new launches, "old" for established coins, "unknown" if unclear
   - CONFIDENCE: "high" if launch announcement matches, "medium" for typical fresh signals, "low" for unclear

Fresh launch indicators: "NEW", "LAUNCH", "JUST", "FRESH", "MINT", "RELEASED", "NOW", "🟢", "🚀", "LIVE"
Old call indicators: "CALL", "GEM", "BUY", "HOLD", "SUPPORT", "ACCUMULATE", "DIP", "🎯", "REVISITING"

Launch announcement patterns to remember:
- "Launching in 2 hours" → expect CA in ~2 hours
- "Going live in 30 minutes" → expect CA in ~30 minutes  
- "CA dropping soon" → expect CA within minutes
- "Live now" + CA = immediate buy signal

Examples:
- Input: "🟢 NEW LAUNCH! CA: DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263" (with prior "launch in 5 min" announcement)
- Output: DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263|fresh|high

- Input: "BUY $PEPE now! Great entry! CA: DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
- Output: DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263|old|medium

- Input: "Great project!" (no CA)
- Output: none|unknown|low

Extract from this message:"""


class ChatMemory:
    """Stores chat history to understand launch timing and context."""
    
    def __init__(self, max_messages: int = 50, max_age_hours: int = 6):
        self.messages: deque = deque(maxlen=max_messages)  # (timestamp, chat_id, text, has_address)
        self.launch_announcements: dict = {}  # chat_id -> list of (timestamp, delay_minutes, token_name)
        self.max_age = timedelta(hours=max_age_hours)
    
    def add_message(self, chat_id: int, text: str, has_address: bool = False) -> None:
        """Store a message with timestamp."""
        now = datetime.utcnow()
        self.messages.append({
            'timestamp': now,
            'chat_id': chat_id,
            'text': text[:200],  # Truncate for memory
            'has_address': has_address
        })
        
        # Check for launch announcements
        self._detect_launch_announcement(chat_id, text, now)
    
    def _detect_launch_announcement(self, chat_id: int, text: str, timestamp: datetime) -> None:
        """Detect 'launching in X minutes/hours' patterns."""
        import re
        text_lower = text.lower()
        
        # Patterns like "launch in 30 minutes", "live in 2 hours", "ca dropping in 15 min"
        patterns = [
            r'(?:launch|live|ca|contract|going|dropping)\s+(?:in|at)\s+(\d+)\s*(?:min|minute)',
            r'(?:launch|live|ca|contract|going|dropping)\s+(?:in|at)\s+(\d+)\s*(?:hr|hour)',
            r'(\d+)\s*(?:min|minute)s?\s+(?:until|till|to)\s+(?:launch|live|ca|contract)',
            r'(\d+)\s*(?:hr|hour)s?\s+(?:until|till|to)\s+(?:launch|live|ca|contract)',
            r'(?:launch|live)\s+(?:in|at)\s+(\d+):(\d+)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text_lower)
            if match:
                # Extract delay
                if 'hour' in text_lower or 'hr' in text_lower:
                    delay_minutes = int(match.group(1)) * 60
                else:
                    delay_minutes = int(match.group(1))
                
                # Try to extract token name (look for $XXX or ALLCAPS)
                token_match = re.search(r'\$([A-Z]{2,10})|#([A-Z]{2,10})', text)
                token_name = token_match.group(1) or token_match.group(2) if token_match else "unknown"
                
                if chat_id not in self.launch_announcements:
                    self.launch_announcements[chat_id] = []
                
                self.launch_announcements[chat_id].append({
                    'timestamp': timestamp,
                    'delay_minutes': delay_minutes,
                    'token_name': token_name,
                    'original_text': text[:100]
                })
                
                logger.info("launch_announcement_detected",
                           chat_id=chat_id,
                           token=token_name,
                           delay_minutes=delay_minutes,
                           expected_time=(timestamp + timedelta(minutes=delay_minutes)).strftime('%H:%M'))
                break
    
    def get_recent_context(self, chat_id: int, minutes: int = 30) -> str:
        """Get recent messages from a chat for context."""
        now = datetime.utcnow()
        cutoff = now - timedelta(minutes=minutes)
        
        recent = [
            f"[{m['timestamp'].strftime('%H:%M')}] {m['text']}"
            for m in self.messages
            if m['chat_id'] == chat_id and m['timestamp'] > cutoff
        ]
        
        return "\n".join(recent[-10:])  # Last 10 messages max
    
    def is_expected_launch(self, chat_id: int, token_address: str, tolerance_minutes: int = 15) -> tuple[bool, str]:
        """Check if a token address appearing now matches an expected launch."""
        now = datetime.utcnow()
        
        if chat_id not in self.launch_announcements:
            return False, "no prior announcement"
        
        # Clean old announcements
        self.launch_announcements[chat_id] = [
            ann for ann in self.launch_announcements[chat_id]
            if now - ann['timestamp'] < timedelta(hours=6)  # Keep 6 hours
        ]
        
        if not self.launch_announcements[chat_id]:
            return False, "no recent announcement"
        
        # Check if any announcement's expected time is now (within tolerance)
        for ann in self.launch_announcements[chat_id]:
            expected_time = ann['timestamp'] + timedelta(minutes=ann['delay_minutes'])
            diff = abs((now - expected_time).total_seconds() / 60)
            
            if diff <= tolerance_minutes:
                return True, f"matches {ann['token_name']} launch (expected {expected_time.strftime('%H:%M')}, diff {diff:.0f}min)"
        
        return False, "no matching launch time"
    
    def cleanup_old(self) -> None:
        """Remove old messages and announcements."""
        now = datetime.utcnow()
        
        # Messages auto-cleanup via deque maxlen
        
        # Clean old announcements
        for chat_id in list(self.launch_announcements.keys()):
            self.launch_announcements[chat_id] = [
                ann for ann in self.launch_announcements[chat_id]
                if now - ann['timestamp'] < self.max_age
            ]
            if not self.launch_announcements[chat_id]:
                del self.launch_announcements[chat_id]


class AIGatewayParser:
    """Parse Telegram messages using OpenRouter or any OpenAI-compatible API with chat memory."""
    
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
        
        # Chat memory for context
        self.memory = ChatMemory(max_messages=100, max_age_hours=6)
        
        # Stats
        self.requests_made = 0
        self.timeouts = 0
        self.tokens_found = 0
        self.ai_errors = 0
        self.high_confidence_buys = 0
        
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
    
    async def extract_token_with_context(self, message_text: str, chat_id: int) -> tuple[Optional[str], str, str]:
        """
        Extract token with chat context for better launch timing detection.
        Returns (address, classification, confidence) where confidence is "high", "medium", or "low".
        """
        # Store this message
        has_address = len(message_text) > 32 and any(c.isalnum() for c in message_text)
        self.memory.add_message(chat_id, message_text, has_address)
        
        # Check if there's a matching launch announcement
        is_expected_launch, launch_context = self.memory.is_expected_launch(chat_id, "")
        
        # Get recent context
        recent_context = self.memory.get_recent_context(chat_id, minutes=60)
        
        # Build context-aware prompt
        context_prompt = message_text
        if recent_context:
            context_prompt = f"RECENT CHAT HISTORY:\n{recent_context}\n\nCURRENT MESSAGE:\n{message_text}"
        
        # Get result from AI
        address, classification = await self.extract_token(context_prompt)
        
        # Determine confidence based on launch timing match
        confidence = "medium"  # default
        if is_expected_launch and address:
            confidence = "high"
            self.high_confidence_buys += 1
            logger.info("high_confidence_launch_match",
                       token=address[:8] if address else "none",
                       chat=chat_id,
                       context=launch_context)
        
        return address, classification, confidence
    
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
    
    async def extract_token_fast(self, message_text: str, chat_id: int = 0) -> tuple[Optional[str], str, str]:
        """
        Fast extraction with chat context and fallback to regex if AI fails.
        First tries AI with context, falls back to regex pattern matching if timeout/error.
        Returns (address, classification, confidence) where confidence is "high", "medium", or "low".
        """
        # Try AI with context first (if chat_id provided)
        if chat_id != 0:
            address, classification, confidence = await self.extract_token_with_context(message_text, chat_id)
            if address:
                return address, classification, confidence
        else:
            # No chat context available
            address, classification = await self.extract_token(message_text)
            if address:
                return address, classification, "medium"
        
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
                    return match, "unknown", "low"
            # If no short ones, return the first
            return matches[0], "unknown", "low"
        
        return None, "unknown", "low"
    
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
