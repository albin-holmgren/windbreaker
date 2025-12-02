"""
Main entry point for Pulse Sniper.

Usage:
    python -m src.main_sniper
"""

import asyncio
import structlog
from src.pulse_sniper import main

# Configure logging
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
    ],
    wrapper_class=structlog.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

if __name__ == "__main__":
    asyncio.run(main())
