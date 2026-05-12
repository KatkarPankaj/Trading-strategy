"""Point yfinance cache (tz + cookies) at a project-local dir before first use.

Avoids failures when ~/Library/Caches/py-yfinance is missing, not a directory,
or otherwise unusable (Errno 17 etc.). Override with env YFINANCE_CACHE_DIR.
"""

from __future__ import annotations

import os
from pathlib import Path

_done = False


def configure_yfinance_cache() -> None:
    global _done
    if _done:
        return
    try:
        import yfinance as yf
    except ImportError:
        _done = True
        return
    root = Path(__file__).resolve().parents[2]
    loc = Path(os.environ.get("YFINANCE_CACHE_DIR", root / ".cache" / "py-yfinance"))
    try:
        loc.mkdir(parents=True, exist_ok=True)
        yf.set_tz_cache_location(str(loc))
    except Exception:
        pass
    _done = True


configure_yfinance_cache()
