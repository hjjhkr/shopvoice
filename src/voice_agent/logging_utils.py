"""Append-only JSONL audit logs with secret redaction.

Three logs are written under LOG_DIR:
  prompts.jsonl        every system/user prompt sent to the LLM, per node
  mcp_tool_calls.jsonl every MCP tool request/response (written by the server)
  agent_runs.jsonl     one record per agent run (plan, tool calls, citations)
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import get_settings

_lock = threading.Lock()

_SECRET_ENV_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),           # OpenAI / Anthropic style
    re.compile(r"\bBSA[A-Za-z0-9_\-]{20,}\b"),        # Brave
    re.compile(r"\btvly-[A-Za-z0-9_\-]{10,}"),        # Tavily
    re.compile(r"\b[A-Fa-f0-9]{40,}\b"),              # long hex tokens (Serper etc.)
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
]


def _secret_values() -> list[str]:
    return [
        v for k, v in os.environ.items()
        if any(h in k.upper() for h in _SECRET_ENV_HINTS) and len(v) >= 8
    ]


def redact(text: str) -> str:
    """Remove anything that looks like a credential from text."""
    if not isinstance(text, str):
        return text
    for value in _secret_values():
        text = text.replace(value, "[REDACTED]")
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def _redact_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact_obj(v) for v in obj]
    return obj


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def log_event(log_name: str, record: dict[str, Any]) -> None:
    """Append one redacted JSON record to LOG_DIR/<log_name>.jsonl."""
    log_dir: Path = get_settings().log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": now_iso(), **_redact_obj(record)}, ensure_ascii=False, default=str)
    with _lock:
        with open(log_dir / f"{log_name}.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")
