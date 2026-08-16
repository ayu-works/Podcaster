"""Vercel Python entry point."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ONBOARDING = str(ROOT / "block-3-onboarding")
if ONBOARDING not in sys.path:
    sys.path.insert(0, ONBOARDING)

from app import app  # noqa: E402,F401
