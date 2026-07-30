#!/usr/bin/env python3
"""Compatibility entry point for mobile_profiler.maa_roguelike_runner."""

from __future__ import annotations

import sys
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from mobile_profiler.maa_roguelike_runner import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
