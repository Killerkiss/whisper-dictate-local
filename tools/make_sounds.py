#!/usr/bin/env python3
"""Regenerate the cue sounds by hand.

Normally unnecessary: the app regenerates them whenever the lead-in setting
changes. Useful for previewing a value without opening Settings.

Usage: python3 tools/make_sounds.py [lead_in_ms]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ukdictate import sounds  # noqa: E402

lead = int(sys.argv[1]) if len(sys.argv) > 1 else 600
sounds.generate(lead)
for name in sounds.FILES:
    path = sounds.CACHE_DIR / name
    print(f"  {path}  ({path.stat().st_size} bytes)")
