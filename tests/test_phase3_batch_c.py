"""Phase 3 Batch C acceptance tests.

Covers:
  §1 — PTY-backed bash subprocess: lines stream live (no block-buffering),
       /dev/null stdin, tty.cols reports a positive integer.
  §3 — Operator-initiated cancel: BashTool.cancel() SIGTERMs the running
       process group; tool returns within ~3 s with `[cancelled by
       operator]` and `cancelled=True` in the audit `tool_call` event.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from opsbridge.agent import tools as t
from opsbridge.agent.logging import SessionLogger


# ---------------------------------------------------------------------------
# §1 — PTY-backed bash
# ---------------------------------------------------------------------------

class TestPty:
    def test_lines_stream_before_subprocess_exits(self, tmp_path):
        """First line should arrive in the sink WHILE the subprocess is
        still alive — block-buffering on stdout=PIPE would delay it until
        the child closes.
        """
        captured_at: list[tuple[float, str]] = []
        started = time.monotonic()

        def sink(line: str) -> None:
            captured_at.append((time.monotonic() - started, line))

        # 'first', sleep 2, 'second' — first must land within ~1s.
        out, meta = t.tool_bash(
            "printf 'first\\n'; sleep 2; printf 'second\\n'",
            cwd=str(tmp_path),
            line_sink=sink,
            timeout_sec=10,
        )
        assert "first" in out and "second" in out
        first_arrivals = [ts for ts, line in captured_at if "first" in line]
        assert first_arrivals, "first line never reached the sink"
        assert first_arrivals[0] < 1.5, (
            f"first line arrived at t={first_arrivals[0]:.2f}s — output is still "
            "block-buffered; PTY mode isn't working"
        )

    def test_stdin_is_devnull(self, tmp_path):
        """read should see immediate EOF — operator's SSH stdin must not
        bleed into the subprocess.
        """
        out, meta = t.tool_bash(
            "read -r REPLY; echo \"got=[$REPLY]\"; echo done",
            cwd=str(tmp_path),
            timeout_sec=3,
        )
        assert meta["timeout"] is False, "read blocked — stdin isn't /dev/null"
        assert "got=[]" in out
        assert "done" in out

    def test_child_sees_tty_via_tput_cols(self, tmp_path):
        """tput cols only works when stdin is a TTY — PTY mode satisfies that."""
        out, meta = t.tool_bash("tput cols", cwd=str(tmp_path), timeout_sec=5)
        # Output should contain a digit.
        cols_line = next((l for l in out.strip().splitlines() if l.strip().isdigit()), "")
        assert cols_line and int(cols_line) > 0

    def test_pty_disabled_falls_back_to_pipe(self, tmp_path):
        """Callers can explicitly opt out of PTY for tests / niche needs."""
        out, meta = t.tool_bash("echo hello", cwd=str(tmp_path), use_pty=False)
        assert "hello" in out
        assert meta["exit"] == 0

    def test_pty_does_not_inject_carriage_returns_into_output(self, tmp_path):
        """PTYs canonical-mode add \\r\\n; we normalize to \\n on the way out."""
        out, meta = t.tool_bash("echo hello", cwd=str(tmp_path))
        assert "\r\n" not in out
        # Standalone \r is acceptable for installers that use it for progress
        # bars, but a plain echo should have only \n.
        assert "hello\n" in out


# ---------------------------------------------------------------------------
# §3 — Operator-initiated cancel
# ---------------------------------------------------------------------------

class TestCancel:
    def test_bash_tool_has_cancel_method(self):
        bt = t.BashTool()
        assert hasattr(bt, "cancel")
        assert callable(bt.cancel)

    def test_cancel_when_idle_is_noop(self):
        """Calling cancel() without an active subprocess returns False
        and does not raise.
        """
        bt = t.BashTool()
        assert bt.cancel() is False

    def test_is_running_reflects_active_subprocess(self, tmp_path):
        """is_running flips True while a forward() call is in flight."""
        bt = t.BashTool(cwd=str(tmp_path))
        ready = threading.Event()
        finished = threading.Event()

        def runner() -> None:
            ready.set()
            bt.forward("sleep 0.5")
            finished.set()

        th = threading.Thread(target=runner, daemon=True)
        th.start()
        ready.wait(timeout=2)
        # Wait for the subprocess to actually start (Popen takes ~10–50ms).
        for _ in range(50):
            if bt.is_running:
                break
            time.sleep(0.02)
        assert bt.is_running, "is_running never went True during forward()"
        th.join(timeout=5)
        assert finished.is_set()
        assert not bt.is_running

    def test_cancel_terminates_running_subprocess(self, tmp_path):
        """sleep 30 with cancel() at +0.4s should return within ~3s with
        the `[cancelled by operator]` marker.
        """
        bt = t.BashTool(cwd=str(tmp_path))
        result_box: dict = {}
        started = time.monotonic()

        def runner() -> None:
            result_box["output"] = bt.forward("sleep 30", timeout_sec=60)
            result_box["elapsed"] = time.monotonic() - started

        th = threading.Thread(target=runner, daemon=True)
        th.start()
        time.sleep(0.4)
        signalled = bt.cancel()
        assert signalled is True
        th.join(timeout=6)

        assert not th.is_alive(), "BashTool did not exit within 6s of cancel()"
        assert result_box["elapsed"] < 4.0, (
            f"cancel returned in {result_box['elapsed']:.1f}s — too slow"
        )
        assert "[cancelled by operator]" in result_box["output"]

    def test_cancel_audit_event_has_flag(self, tmp_path):
        log = SessionLogger(log_dir=tmp_path / "logs")
        bt = t.BashTool(logger=log, cwd=str(tmp_path))

        def runner() -> None:
            bt.forward("sleep 30", timeout_sec=60)

        th = threading.Thread(target=runner, daemon=True)
        th.start()
        time.sleep(0.4)
        bt.cancel()
        th.join(timeout=6)
        log.close()

        events = [json.loads(l) for l in log.path.read_text().splitlines() if l.strip()]
        tc = next(e for e in events if e["event"] == "tool_call" and e.get("tool") == "bash")
        assert tc.get("cancelled") is True
        assert tc.get("timeout") is False

    def test_cancel_event_clears_between_calls(self, tmp_path):
        """A cancelled run must NOT leave the cancel flag set for the
        next call — otherwise the next subprocess would auto-cancel.
        """
        bt = t.BashTool(cwd=str(tmp_path))

        def runner() -> None:
            bt.forward("sleep 30", timeout_sec=60)

        th = threading.Thread(target=runner, daemon=True)
        th.start()
        time.sleep(0.4)
        bt.cancel()
        th.join(timeout=6)

        # New call right after — should NOT be cancelled.
        out = bt.forward("echo survived", timeout_sec=5)
        assert "survived" in out
        assert "cancelled" not in out

    def test_cancel_during_natural_exit_is_harmless(self, tmp_path):
        """Cancel called right as the subprocess naturally exits — no crash."""
        bt = t.BashTool(cwd=str(tmp_path))
        bt.forward("echo fast")
        # cancel after-the-fact is a no-op.
        assert bt.cancel() is False
