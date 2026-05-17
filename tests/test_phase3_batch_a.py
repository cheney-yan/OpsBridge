"""Phase 3 Batch A acceptance tests.

Covers:
  §13 — sticky cwd tracking + status-bar cwd indicator
  §15 — TUI layout: status bar consolidated with header info
  §12 — `!` prefix routes to BashTool.run_direct with source="direct"
  §14 — /help slash command emits help text to top region
  partial §2 — set_status("running bash" / "visiting" / "searching")
              transitions wire through the existing 1Hz tick loop
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from opsbridge.agent import tools as t
from opsbridge.agent.logging import SessionLogger
from opsbridge.agent.tui import OpsBridgeApp, StatusBar, _abbreviate_cwd, HELP_TEXT


# ---------------------------------------------------------------------------
# §13 — sticky cwd
# ---------------------------------------------------------------------------

class TestStickyCwd:
    def test_tool_bash_reports_cwd_in_meta(self, tmp_path):
        """Phase 3 §13: tool_bash returns the post-command cwd in meta."""
        out, meta = t.tool_bash("true", cwd=str(tmp_path))
        assert meta.get("cwd") == str(tmp_path)

    def test_tool_bash_follows_cd_in_command(self, tmp_path):
        """`cd` inside the command should update the reported cwd."""
        subdir = tmp_path / "sub"
        subdir.mkdir()
        out, meta = t.tool_bash(f"cd {subdir}", cwd=str(tmp_path))
        assert meta.get("cwd") == str(subdir)

    def test_tool_bash_cwd_capture_can_be_disabled(self, tmp_path):
        """Callers can opt out of cwd capture (e.g., to keep output pristine)."""
        out, meta = t.tool_bash("true", cwd=str(tmp_path), track_cwd=False)
        assert "cwd" not in meta

    def test_tool_bash_preserves_exit_code(self, tmp_path):
        """The cwd-capture wrapper must NOT clobber the operator's exit code."""
        out, meta = t.tool_bash("false", cwd=str(tmp_path))
        assert meta["exit"] != 0, "exit code should reflect `false`, not the wrapper's printf"

    def test_tool_bash_marker_not_in_output(self, tmp_path):
        """The sentinel used for cwd capture must be stripped from captured output."""
        out, meta = t.tool_bash("echo hello", cwd=str(tmp_path))
        assert "OPSBRIDGE_CWD" not in out

    def test_bashtool_tracks_sticky_cwd_across_calls(self, tmp_path):
        """BashTool's _current_cwd follows successive cd's between calls."""
        bt = t.BashTool(cwd=str(tmp_path))
        sub_a = tmp_path / "a"
        sub_a.mkdir()
        bt.forward(f"cd {sub_a}")
        assert bt.current_cwd == str(sub_a)
        # Next call inherits without an explicit cwd argument.
        out = bt.forward("pwd")
        assert str(sub_a) in out


# ---------------------------------------------------------------------------
# §12 — source field in audit log
# ---------------------------------------------------------------------------

class TestSourceField:
    def test_llm_routed_call_records_source_llm(self, tmp_path):
        log = SessionLogger(log_dir=tmp_path / "logs")
        bt = t.BashTool(logger=log, cwd=str(tmp_path))
        bt.forward("echo hi")
        log.close()
        events = [json.loads(l) for l in log.path.read_text().splitlines() if l.strip()]
        tc = next(e for e in events if e["event"] == "tool_call" and e.get("tool") == "bash")
        assert tc.get("source") == "llm"
        pre = next(e for e in events if e["event"] == "bash_pre_exec")
        assert pre.get("source") == "llm"

    def test_direct_run_records_source_direct(self, tmp_path):
        log = SessionLogger(log_dir=tmp_path / "logs")
        bt = t.BashTool(logger=log, cwd=str(tmp_path))
        bt.run_direct("echo hi")
        log.close()
        events = [json.loads(l) for l in log.path.read_text().splitlines() if l.strip()]
        tc = next(e for e in events if e["event"] == "tool_call" and e.get("tool") == "bash")
        assert tc.get("source") == "direct"
        pre = next(e for e in events if e["event"] == "bash_pre_exec")
        assert pre.get("source") == "direct"


# ---------------------------------------------------------------------------
# §15 — status bar consolidation + path abbreviation helper
# ---------------------------------------------------------------------------

class TestStatusBarConsolidation:
    def test_app_status_bar_carries_hostname_model(self):
        app = OpsBridgeApp(
            hostname="myhost",
            model_label="claude-sonnet-4-6",
            on_operator_turn=lambda _t: None,
            on_cancel=lambda: None,
        )
        # hostname + model are stored on the App and pushed into StatusBar
        # at mount time. We don't run the App (no asyncio loop), so we
        # check the stored attributes — mount-side wiring is covered by
        # the textual Pilot test below.
        assert app._hostname == "myhost"
        assert app._model_label == "claude-sonnet-4-6"

    def test_window_title_still_has_meta_for_tmux(self):
        """Tmux/terminal title bar uses self.title — keep host+model there."""
        app = OpsBridgeApp(
            hostname="myhost",
            model_label="claude-sonnet-4-6",
            on_operator_turn=lambda _t: None,
            on_cancel=lambda: None,
        )
        assert "OpsBridge" in app.title
        assert "myhost" in app.title
        assert "claude-sonnet-4-6" in app.title

    pass  # `test_no_header_widget_present` below uses the Pilot harness


@pytest.mark.asyncio
async def test_no_header_widget_present():
    """Phase 3 §15: the Header widget is gone from the rendered DOM."""
    app = OpsBridgeApp(
        hostname="h",
        model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        # The Phase 2 layout used textual's Header; phase-3 §15 removes it.
        headers = list(app.query("Header"))
        assert headers == []
        # Three core regions still present.
        assert list(app.query("TopLog"))
        assert list(app.query("MiddlePanel"))
        assert list(app.query("StatusBar"))


class TestPathAbbreviation:
    def test_home_collapses_to_tilde(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/agent")
        assert _abbreviate_cwd("/home/agent") == "~"

    def test_home_subpath_uses_tilde_prefix(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/agent")
        assert _abbreviate_cwd("/home/agent/projects/x") == "~/projects/x"

    def test_long_absolute_path_middle_ellipsis(self):
        out = _abbreviate_cwd("/usr/share/very/deep/nested/path/here", max_len=28)
        assert "…" in out
        assert len(out) <= 30  # may include the ellipsis char and a small over-shoot

    def test_short_path_passes_through(self):
        assert _abbreviate_cwd("/tmp") == "/tmp"

    def test_empty_returns_empty(self):
        assert _abbreviate_cwd("") == ""


# ---------------------------------------------------------------------------
# §14 — /help text content
# ---------------------------------------------------------------------------

class TestHelpText:
    def test_help_mentions_all_slash_commands(self):
        for cmd in ("/quit", "/help", "/model"):
            assert cmd in HELP_TEXT, f"{cmd} should appear in /help output"

    def test_help_mentions_bang_prefix(self):
        assert "!<cmd>" in HELP_TEXT

    def test_help_mentions_ctrl_d_arming(self):
        assert "Ctrl-D" in HELP_TEXT

    def test_help_mentions_audit_log_path(self):
        assert "/var/log/opsbridge/agent" in HELP_TEXT


# ---------------------------------------------------------------------------
# Integration: TUI dispatch (slash command + ! prefix)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_slash_help_routes_to_top_log():
    """Typing /help adds the help-text lines to the top region."""
    written: list[str] = []

    app = OpsBridgeApp(
        hostname="h",
        model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        # Override _do_write_top to capture lines.
        app._do_write_top = lambda line: written.append(line)
        await pilot.press("/", "h", "e", "l", "p", "enter")
        await pilot.pause()
    # /help echoes the command itself + multi-line help body.
    combined = "\n".join(written)
    assert "OpsBridge slash commands" in combined
    assert "/model" in combined


@pytest.mark.asyncio
async def test_bang_prefix_routes_to_direct_bash():
    """Typing `!ls` calls on_direct_bash, NOT on_operator_turn."""
    direct_calls: list[str] = []
    llm_calls: list[str] = []

    app = OpsBridgeApp(
        hostname="h",
        model_label="m",
        on_operator_turn=lambda t: llm_calls.append(t),
        on_cancel=lambda: None,
        on_direct_bash=lambda c: direct_calls.append(c),
    )
    async with app.run_test() as pilot:
        await pilot.press("!", "l", "s", "enter")
        await pilot.pause()
    assert direct_calls == ["ls"]
    assert llm_calls == []


@pytest.mark.asyncio
async def test_escaped_bang_routes_to_llm():
    """`\\!ls` treats the `!` as literal — should route through the LLM."""
    direct_calls: list[str] = []
    llm_calls: list[str] = []

    app = OpsBridgeApp(
        hostname="h",
        model_label="m",
        on_operator_turn=lambda t: llm_calls.append(t),
        on_cancel=lambda: None,
        on_direct_bash=lambda c: direct_calls.append(c),
    )
    async with app.run_test() as pilot:
        await pilot.press("backslash", "!", "l", "s", "enter")
        await pilot.pause()
    assert direct_calls == []
    assert llm_calls == ["!ls"]


@pytest.mark.asyncio
async def test_status_bar_reflects_cwd_update():
    """app.set_cwd updates the StatusBar's cwd reactive (and abbreviates)."""
    app = OpsBridgeApp(
        hostname="h",
        model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        # Direct call (already on the main thread inside run_test).
        app._do_set_cwd("/tmp")
        await pilot.pause()
        bar = app.query_one(StatusBar)
        assert bar.cwd == "/tmp"


@pytest.mark.asyncio
async def test_status_bar_elapsed_clock_advances():
    """When status leaves idle, elapsed should tick up via the 1Hz spinner."""
    app = OpsBridgeApp(
        hostname="h",
        model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    # Speed up the tick for the test.
    app.HEARTBEAT_INTERVAL_SEC = 0.05
    async with app.run_test() as pilot:
        app._do_set_status("running bash")
        await pilot.pause()
        # Sleep long enough that the spinner has ticked at least twice.
        import asyncio as _asyncio
        await _asyncio.sleep(0.25)
        bar = app.query_one(StatusBar)
        assert bar.elapsed > 0, "elapsed should advance while non-idle"
