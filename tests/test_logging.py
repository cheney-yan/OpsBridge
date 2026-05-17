"""Tests for SessionLogger — schema invariants per PRD §5 / TEST.md T6.7."""
from __future__ import annotations

import json
from pathlib import Path

from opsbridge.agent.logging import SessionLogger


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_session_start_end(tmp_path):
    log = SessionLogger(log_dir=tmp_path)
    log.emit("session_start", ssh_key_fingerprint="SHA256:abc", provider="openai", model="gpt-4o", base_url="")
    log.emit("session_end", reason="clean", turn_count=2)
    log.close()
    recs = _read_jsonl(log.path)
    assert recs[0]["event"] == "session_start"
    assert recs[0]["ssh_key_fingerprint"] == "SHA256:abc"
    assert recs[-1]["event"] == "session_end"
    assert recs[-1]["reason"] == "clean"
    assert recs[-1]["turn_count"] == 2


def test_tool_call_truncation(tmp_path):
    log = SessionLogger(log_dir=tmp_path)
    huge = "x" * 100_000
    log.emit("tool_call", tool="bash", args={"command": huge}, result=huge, duration_ms=5)
    log.close()
    recs = _read_jsonl(log.path)
    rec = recs[0]
    assert rec["event"] == "tool_call"
    assert len(rec["args"]["command"]) < len(huge)
    assert len(rec["result"]) < len(huge)
    assert rec["duration_ms"] == 5


def test_falls_back_to_stderr_when_unwritable(tmp_path, capsys):
    """If log dir can't be created we degrade to stderr, not crash."""
    # Point at a place we don't own under /proc.
    log = SessionLogger(log_dir=Path("/proc/nonexistent/log"))
    # Should not raise even though the dir isn't writable.
    log.emit("session_start", ssh_key_fingerprint="x", provider="openai", model="m", base_url="")
    log.close()
