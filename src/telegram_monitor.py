"""
Telegram User Monitor - Monitors all Telegram groups using Telethon.
Streams messages in real-time and extracts potential token signals.
"""

import asyncio
import re
import base64
import gzip
import os
from typing import Optional, Callable, List, Set
from datetime import datetime
import structlog

from telethon import TelegramClient, events
from telethon.tl.types import Channel, Chat, User
from telethon.sessions import StringSession

logger = structlog.get_logger(__name__)

# Regex for Solana addresses (base58, 32-44 chars)
SOLANA_ADDRESS_PATTERN = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')

# Keywords that often accompany token calls
CRYPTO_KEYWORDS = [
    'buy', 'gem', 'moon', 'pump', '100x', '1000x', 'ath', 'breakout',
    'early', 'alpha', 'ca', 'contract', 'address', 'mint', 'token',
    'shill', 'ape', 'degens', 'degen', 'wagmi', 'fomo', 'hodl',
    'dev', 'based', 'safu', 'dyor', ' NFA', ' NFA,', 'NFA ',
    'signal', 'call', 'entry', 'target', 'stop', 'profit',
    'new', 'fair', 'launch', 'presale', 'ido', 'ico'
]


class TelegramUserMonitor:
    """Monitor all Telegram groups using a user account via Telethon."""
    
    def __init__(
        self,
        api_id: int,
        api_hash: str,
        phone: str,
        session_name: str = "telegram_trader_session",
        on_message: Optional[Callable[[str, str, str], None]] = None,
    ):
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone = phone
        self.session_name = session_name
        self.on_message = on_message
        
        self.client: Optional[TelegramClient] = None
        self.monitored_groups: Set[int] = set()
        self.running = False
        self.startup_time: Optional[datetime] = None
        
        # Stats
        self.messages_received = 0
        self.potential_signals = 0
        
    async def start(self) -> None:
        """Start the Telegram client and begin monitoring."""
        logger.info("starting_telegram_monitor", session=self.session_name)
        
        # Check for compressed session in environment (Railway deployment)
        session_gz_b64 = os.environ.get("TELEGRAM_SESSION_GZ")
        railway_session_path = f"/data/{self.session_name}.session"
        
        if session_gz_b64 and not os.path.exists(railway_session_path):
            try:
                logger.info("decompressing_session_from_env")
                session_bytes = gzip.decompress(base64.b64decode(session_gz_b64))
                os.makedirs("/data", exist_ok=True)
                with open(railway_session_path, "wb") as f:
                    f.write(session_bytes)
                logger.info("session_saved", path=railway_session_path, size=len(session_bytes))
            except Exception as e:
                logger.error("failed_to_decompress_session", error=str(e))
        
        # Check for session string in environment
        session_string = os.environ.get("TELEGRAM_SESSION_STRING")
        if session_string:
            logger.info("using_session_from_env")
            session = StringSession(session_string)
        else:
            # Check for session file in /data (Railway volume) first, then local
            if os.path.exists(railway_session_path):
                logger.info("using_railway_session", path=railway_session_path)
                session = railway_session_path.replace('.session', '')
            else:
                logger.info("using_local_session", file=self.session_name)
                session = self.session_name
        
        self.client = TelegramClient(
            session,
            self.api_id,
            self.api_hash
        )
        
        if session_string:
            # StringSession is already authenticated, just connect
            await self.client.connect()
            if not await self.client.is_user_authorized():
                logger.error("telegram_session_string_not_authorized")
                raise ValueError("TELEGRAM_SESSION_STRING is invalid or expired - regenerate it")
        else:
            await self.client.start(phone=self.phone)
            if not await self.client.is_user_authorized():
                logger.error("telegram_not_authorized")
                raise ValueError("Telegram login failed - check phone number and code")
        
        me = await self.client.get_me()
        logger.info("telegram_logged_in", user=me.username or me.phone)
        
        # Discover all groups/channels
        await self._discover_groups()
        
        # Set startup time AFTER discovery (skip any burst from initial connection)
        self.startup_time = datetime.now()
        logger.info("startup_time_set", wait_seconds=3, message="Skipping messages for 3 seconds to avoid burst")
        await asyncio.sleep(3)
        
        # Set up message handler (also use as backup for real-time events)
        @self.client.on(events.NewMessage())
        async def handle_new_message(event):
            await self._process_message(event)
        
        self.running = True
        logger.info("telegram_monitor_started", groups=len(self.monitored_groups))
        
        # Verify handler is registered
        logger.info("handler_registered", handlers_count=len(self.client._event_builders))
        
        # Force Telegram to sync updates
        logger.info("syncing_updates_with_telegram")
        try:
            await self.client.catch_up()
            logger.info("updates_synced")
        except Exception as e:
            logger.warning("catch_up_failed", error=str(e))
        
        # Test: Try to read last message from first monitored group to verify access
        if self.monitored_groups:
            try:
                test_chat_id = list(self.monitored_groups)[0]
                logger.info("testing_group_access", chat_id=test_chat_id)
                async for message in self.client.iter_messages(test_chat_id, limit=1):
                    if message:
                        logger.info("group_access_ok", 
                                   chat_id=test_chat_id,
                                   last_message_time=str(message.date),
                                   has_text=bool(message.text))
                        break
            except Exception as e:
                logger.error("group_access_failed", chat_id=test_chat_id, error=str(e))
        
        # Start a health check task
        health_task = asyncio.create_task(self._health_check())
        
        # Start polling loop (primary mechanism since push updates don't work reliably across IPs)
        poll_task = asyncio.create_task(self._poll_groups())
        
        # Use Telethon's recommended way to keep receiving updates (as backup)
        try:
            await self.client.run_until_disconnected()
        except Exception as e:
            logger.error("run_until_disconnected_error", error=str(e))
        finally:
            health_task.cancel()
            poll_task.cancel()
            logger.warning("telegram_monitor_exited")
    
    async def _health_check(self) -> None:
        """Periodic health check to verify updates are being received."""
        last_count = 0
        while self.running:
            await asyncio.sleep(30)  # Check every 30 seconds
            if not self.running:
                break
            
            # Log current stats
            current_count = self.messages_received
            new_messages = current_count - last_count
            last_count = current_count
            
            logger.info("health_check",
                       messages_received=current_count,
                       new_in_last_30s=new_messages,
                       potential_signals=self.potential_signals,
                       is_connected=self.client.is_connected() if self.client else False,
                       is_authorized=await self.client.is_user_authorized() if self.client else False)
            
            if new_messages == 0:
                logger.warning("no_messages_received_in_30s", 
                              hint="Check if Telegram session is valid and groups are active")
    
    async def _poll_groups(self) -> None:
        """Poll groups periodically to fetch new messages."""
        logger.info("polling_started", interval_sec=5)
        
        # Track last seen message ID per chat
        last_message_ids: dict[int, int] = {}
        
        # Initial fetch to establish baseline
        for chat_id in list(self.monitored_groups):
            try:
                async for message in self.client.iter_messages(chat_id, limit=1):
                    if message:
                        last_message_ids[chat_id] = message.id
                        break
            except Exception as e:
                logger.debug("initial_poll_failed", chat_id=chat_id, error=str(e))
        
        logger.info("baseline_established", chats_tracked=len(last_message_ids))
        
        while self.running:
            await asyncio.sleep(5)  # Poll every 5 seconds
            if not self.running:
                break
            
            for chat_id in list(self.monitored_groups):
                try:
                    # Fetch new messages since last seen
                    last_id = last_message_ids.get(chat_id, 0)
                    
                    new_messages = []
                    async for message in self.client.iter_messages(chat_id, min_id=last_id, limit=10):
                        if message.id > last_id:
                            new_messages.append(message)
                    
                    if new_messages:
                        # Update last seen ID
                        last_message_ids[chat_id] = max(m.id for m in new_messages)
                        
                        # Process messages (oldest first)
                        for message in reversed(new_messages):
                            # Create a fake event structure for _process_message
                            class FakeEvent:
                                def __init__(self, msg, chat):
                                    self.message = msg
                                    self.chat_id = chat
                            
                            await self._process_message(FakeEvent(message, chat_id))
                        
                        logger.info("polled_new_messages", 
                                   chat_id=chat_id, 
                                   count=len(new_messages),
                                   latest_id=last_message_ids[chat_id])
                        
                except Exception as e:
                    logger.debug("poll_error", chat_id=chat_id, error=str(e))
    
    async def _discover_groups(self) -> None:
        """Discover all groups and channels the user is in."""
        logger.info("discovering_groups")
        
        async for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            
            # Only monitor groups and channels (not private chats)
            if isinstance(entity, (Channel, Chat)):
                self.monitored_groups.add(dialog.id)
                group_type = "channel" if isinstance(entity, Channel) and entity.broadcast else "group"
                logger.debug("found_group", 
                           id=dialog.id, 
                           name=dialog.name,
                           type=group_type)
        
        logger.info("groups_discovered", count=len(self.monitored_groups))
    
    async def _process_message(self, event) -> None:
        """Process a new message."""
        try:
            # Log EVERY incoming event for debugging (INFO level to ensure visibility)
            logger.info("raw_event_received",
                        chat_id=event.chat_id,
                        has_text=bool(event.message and event.message.text),
                        in_monitored=event.chat_id in self.monitored_groups,
                        monitored_count=len(self.monitored_groups))
            
            # Skip if not from monitored group
            if event.chat_id not in self.monitored_groups:
                logger.info("chat_not_monitored", chat_id=event.chat_id, 
                           sample_monitored=list(self.monitored_groups)[:3])
                return
            
            # Skip if no message text
            if not event.message or not event.message.text:
                return
            
            # Skip messages from before startup (safety check)
            if event.message.date and self.startup_time:
                from datetime import timezone
                msg_time = event.message.date.replace(tzinfo=timezone.utc) if not event.message.date.tzinfo else event.message.date
                startup = self.startup_time.replace(tzinfo=timezone.utc) if not self.startup_time.tzinfo else self.startup_time
                if msg_time < startup:
                    logger.debug("skipping_old_message", msg_time=str(msg_time), startup=str(startup))
                    return
            
            self.messages_received += 1
            
            text = event.message.text
            chat_name = "unknown"
            
            try:
                chat = await event.get_chat()
                chat_name = getattr(chat, 'title', None) or getattr(chat, 'username', 'unknown')
            except Exception:
                pass
            
            # Log every message from monitored groups
            logger.info("group_message_received",
                       chat=chat_name,
                       chat_id=event.chat_id,
                       text_len=len(text),
                       text_preview=text[:80])
            
            # Check if message contains potential crypto signal
            has_address = bool(SOLANA_ADDRESS_PATTERN.search(text))
            has_keywords = any(kw.lower() in text.lower() for kw in CRYPTO_KEYWORDS)
            
            # Trigger on address alone OR address+keywords (relaxed filter)
            if has_address:
                self.potential_signals += 1
                
                logger.info("potential_signal_detected",
                           chat=chat_name,
                           has_address=has_address,
                           has_keywords=has_keywords,
                           text_preview=text[:100])
                
                # Extract all potential addresses
                addresses = SOLANA_ADDRESS_PATTERN.findall(text)
                
                if self.on_message and addresses:
                    # Send to handler with first address
                    await self.on_message(text, addresses[0], chat_name)
            
        except Exception as e:
            logger.error("message_processing_error", error=str(e))
    
    async def stop(self) -> None:
        """Stop monitoring and disconnect."""
        logger.info("stopping_telegram_monitor")
        self.running = False
        
        if self.client:
            await self.client.disconnect()
    
    def get_stats(self) -> dict:
        """Get monitoring stats."""
        return {
            "messages_received": self.messages_received,
            "potential_signals": self.potential_signals,
            "monitored_groups": len(self.monitored_groups)
        }
