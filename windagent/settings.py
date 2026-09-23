"""Small non-executable .env reader; environment variables always take priority."""
from __future__ import annotations
import os
from pathlib import Path


def load_env(path: Path):
    if not path.exists():
        return
    allowed = {"WINDAGENT_CHAT_PROVIDER", "OPENAI_API_KEY", "OPENAI_MODEL", "WINDAGENT_STRICT_AS_OF", "WINDAGENT_LIVE_POLL"}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (item.strip() for item in line.split("=", 1))
        if key not in allowed:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)
