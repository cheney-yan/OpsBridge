"""JSONL session logging.

One file per SSH session under /var/log/opsbridge/agent/<session-id>.jsonl.
Lines are append-only; the file is created with mode 0640 so the agent
group can read but not other users. Truncation of giant args/results
happens here so the log stays scannable.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

LOG_DIR = Path("/var/log/opsbridge/agent")
# Per PRD §10: <session-id>.jsonl. We use a uuid4 + start timestamp for
# easy chronological sorting.
MAX_ARG_BYTES = 4096
MAX_RESULT_BYTES = 8192


def _truncate(value: Any, limit: int) -> Any:
    """Return a JSON-safe value truncated to `limit` bytes when serialized as a string."""
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, dict):
        return {k: _truncate(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_truncate(v, limit) for v in value]
    s = str(value)
    encoded = s.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return s
    head = encoded[: limit - 32].decode("utf-8", errors="replace")
    return f"{head}…[truncated {len(encoded) - limit + 32} bytes]"


class SessionLogger:
    """JSONL session logger. Falls back to stderr if the log directory isn't writable."""

    def __init__(self, log_dir: Path | None = None, session_id: str | None = None) -> None:
        self.session_id = session_id or f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
        self.log_dir = log_dir or LOG_DIR
        self._file = None
        self._fallback = False
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            path = self.log_dir / f"{self.session_id}.jsonl"
            # 0640: rw for root, r for agent group, none for other.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
            self._file = os.fdopen(fd, "a", buffering=1, encoding="utf-8")
            self.path = path
        except OSError as exc:
            self._fallback = True
            self.path = None
            print(f"[opsbridge] warning: log dir not writable: {exc}", file=sys.stderr)

    def emit(self, event: str, **fields: Any) -> None:
        record: dict[str, Any] = {
            "ts": time.time(),
            "session_id": self.session_id,
            "event": event,
        }
        # Truncation: keep `args` and `result` from blowing up the log.
        for k, v in fields.items():
            if k in ("args",):
                record[k] = _truncate(v, MAX_ARG_BYTES)
            elif k in ("result", "diff"):
                record[k] = _truncate(v, MAX_RESULT_BYTES)
            else:
                record[k] = v
        line = json.dumps(record, ensure_ascii=False, default=str)
        if self._file is not None:
            self._file.write(line + "\n")
            self._file.flush()
        else:
            print(line, file=sys.stderr)

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
