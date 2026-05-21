from __future__ import annotations

import json
import time
from typing import Any

_AGENT_DBG_PATH = "/Users/garya/Documents/Work/Learn/python/Trading-strategy/.cursor/debug-d6b078.log"


def agent_dbg_log(hypothesis_id: str, location: str, message: str, data: dict[str, Any]) -> None:
    try:
        with open(_AGENT_DBG_PATH, "a", encoding="utf-8") as _af:
            _af.write(
                json.dumps(
                    {
                        "sessionId": "d6b078",
                        "hypothesisId": hypothesis_id,
                        "location": location,
                        "message": message,
                        "data": data,
                        "timestamp": int(time.time() * 1000),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass
