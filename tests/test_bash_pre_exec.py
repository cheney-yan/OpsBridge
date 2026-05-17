"""Phase 2 audit-log invariants for bash_pre_exec."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from opsbridge.agent import tools as t
from opsbridge.agent.logging import SessionLogger


def _read_events(log: SessionLogger) -> list[dict]:
    return [json.loads(line) for line in log.path.read_text().splitlines() if line.strip()]


def test_bash_pre_exec_precedes_tool_call(tmp_path):
    log = SessionLogger(log_dir=tmp_path / "logs")
    bt = t.BashTool(logger=log)
    bt.forward("echo hi", timeout_sec=5)
    log.close()
    events = _read_events(log)
    names = [e["event"] for e in events]
    assert names.index("bash_pre_exec") < names.index("tool_call")
    pre = next(e for e in events if e["event"] == "bash_pre_exec")
    assert pre["command"] == "echo hi"
    assert pre["timeout_sec"] == 5


def test_bash_pre_exec_persists_on_timeout(tmp_path):
    log = SessionLogger(log_dir=tmp_path / "logs")
    bt = t.BashTool(logger=log)
    bt.forward("sleep 5", timeout_sec=1)
    log.close()
    events = _read_events(log)
    pre = [e for e in events if e["event"] == "bash_pre_exec"]
    post = [e for e in events if e["event"] == "tool_call"]
    assert pre, "bash_pre_exec missing on timeout"
    assert post, "tool_call missing on timeout"
    assert post[-1]["timeout"] is True
