"""Unit tests for AskTool — option validation, fallback prompt, audit logging."""
from __future__ import annotations

import io

import pytest

from opsbridge.agent import tools as t


class _Logger:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, **fields):
        self.events.append((event, dict(fields)))


def test_ask_rejects_empty_options():
    logger = _Logger()
    tool = t.AskTool(logger=logger)
    result = tool.forward("ok?", options=[])
    assert "[ask error" in result
    # No ask_pre_exec event should fire for malformed calls.
    assert all(e != "ask_pre_exec" for e, _ in logger.events)


def test_ask_fallback_returns_default_on_empty_input():
    """No TUI, no input → fall back to the first option."""
    logger = _Logger()
    stdin = io.StringIO("\n")
    stderr = io.StringIO()
    tool = t.AskTool(logger=logger, stdin=stdin, stderr=stderr)
    result = tool.forward("Proceed?", options=["yes", "no"])
    assert result == "yes"
    events = [e for e, _ in logger.events]
    assert events[0] == "ask_pre_exec"
    tool_call = next(p for e, p in logger.events if e == "tool_call")
    assert tool_call["chosen"] == "yes"
    assert tool_call["cancelled"] is False


def test_ask_fallback_picks_by_number():
    logger = _Logger()
    stdin = io.StringIO("2\n")
    stderr = io.StringIO()
    tool = t.AskTool(logger=logger, stdin=stdin, stderr=stderr)
    result = tool.forward("Pick:", options=["alpha", "beta", "gamma"])
    assert result == "beta"


def test_ask_fallback_picks_by_text_prefix():
    logger = _Logger()
    stdin = io.StringIO("ga\n")
    stderr = io.StringIO()
    tool = t.AskTool(logger=logger, stdin=stdin, stderr=stderr)
    result = tool.forward("Pick:", options=["alpha", "beta", "gamma"])
    assert result == "gamma"


def test_ask_eof_returns_cancelled():
    logger = _Logger()
    stdin = io.StringIO("")  # immediate EOF
    stderr = io.StringIO()
    tool = t.AskTool(logger=logger, stdin=stdin, stderr=stderr)
    result = tool.forward("Pick:", options=["yes", "no"])
    assert result == t.AskTool.CANCELLED
    tool_call = next(p for e, p in logger.events if e == "tool_call")
    assert tool_call["cancelled"] is True


def test_ask_pre_exec_includes_prompt_and_options():
    logger = _Logger()
    stdin = io.StringIO("yes\n")
    stderr = io.StringIO()
    t.AskTool(logger=logger, stdin=stdin, stderr=stderr).forward(
        "Run sudo apt install nginx?", options=["yes", "no"]
    )
    pre = next(p for e, p in logger.events if e == "ask_pre_exec")
    assert pre["prompt"] == "Run sudo apt install nginx?"
    assert pre["options"] == ["yes", "no"]


def test_ask_app_path_blocks_and_returns_form_choice():
    """When an `app` is wired in, the tool delegates to `app.show_ask_form`
    and returns whatever string the form yielded."""

    class FakeApp:
        def __init__(self):
            self.calls: list[tuple[str, list[str]]] = []

        def show_ask_form(self, prompt, options):
            self.calls.append((prompt, list(options)))
            return options[-1]  # operator picked last option

    logger = _Logger()
    app = FakeApp()
    result = t.AskTool(logger=logger, app=app).forward(
        "Choose backend?", options=["nginx", "caddy", "haproxy"]
    )
    assert result == "haproxy"
    assert app.calls == [("Choose backend?", ["nginx", "caddy", "haproxy"])]
    tool_call = next(p for e, p in logger.events if e == "tool_call")
    assert tool_call["chosen"] == "haproxy"
