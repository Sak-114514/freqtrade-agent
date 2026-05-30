#!/usr/bin/env python3
"""
Compatibility entrypoint for the local Freqtrade Trading Copilot Agent.

Run with:
    python tools/freqtrade_agent_server.py
"""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_platform.main import app, run  # noqa: F401


if __name__ == "__main__":
    run()
