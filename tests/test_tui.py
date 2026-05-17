"""Smoke tests for the textual TUI App.

Full textual rendering tests need `pytest-textual-snapshot`; we keep these
lean: instantiate the App, hit a few public methods, exercise the form
state. No interactive Pilot loop in unit tests — that's covered by
integration runs.
"""
from __future__ import annotations

import threading

import pytest

from opsbridge.agent.tui import OpsBridgeApp, _AskState, _render_form


def test_app_constructs():
    app = OpsBridgeApp(
        hostname="testhost",
        model_label="gpt-test",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    assert app.title.startswith("OpsBridge")
    assert "testhost" in app.title
    assert "gpt-test" in app.title


def test_form_renders_options_with_default_marker():
    state = _AskState(
        prompt="Proceed?",
        options=["yes", "no"],
        event=threading.Event(),
        chosen={},
        selected_idx=1,
    )
    rendered = _render_form(state)
    assert "Proceed?" in rendered
    # idx=1 → 'no' is the bullet; 'yes' carries the default marker.
    lines = rendered.splitlines()
    yes_line = next(l for l in lines if "yes" in l)
    no_line = next(l for l in lines if "no" in l)
    assert "•" in no_line
    assert "default" in yes_line


def test_form_supports_three_options():
    state = _AskState(
        prompt="Pick:",
        options=["nginx", "caddy", "haproxy"],
        event=threading.Event(),
        chosen={},
        selected_idx=0,
    )
    rendered = _render_form(state)
    assert "nginx" in rendered
    assert "caddy" in rendered
    assert "haproxy" in rendered


def test_write_top_buffered_when_app_not_running(capsys):
    """When the App isn't mounted, write_top should fall back to stdout."""
    app = OpsBridgeApp(
        hostname="h",
        model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    # No event loop running yet → RuntimeError on call_from_thread → fallback print.
    app.write_top("hello fallback")
    out = capsys.readouterr().out
    assert "hello fallback" in out


async def test_quit_requires_double_ctrl_d():
    """Single Ctrl-D arms; second Ctrl-D within the window exits."""
    app = OpsBridgeApp(
        hostname="h",
        model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        await pilot.press("ctrl+d")
        # Armed but not exited.
        assert app._quit_armed_at is not None
        assert app.is_running
        # Second press within the window → exit.
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert not app.is_running


async def test_quit_disarmed_by_input():
    """Typing in the input clears the quit-armed state."""
    app = OpsBridgeApp(
        hostname="h",
        model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        await pilot.press("ctrl+d")
        assert app._quit_armed_at is not None
        # Type something, hit enter.
        await pilot.press("h", "i", "enter")
        await pilot.pause()
        assert app._quit_armed_at is None
        assert app.is_running


async def test_slash_quit_command_exits():
    """Typing `/quit` and pressing Enter ends the session."""
    app = OpsBridgeApp(
        hostname="h",
        model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        await pilot.press("/", "q", "u", "i", "t", "enter")
        await pilot.pause()
        assert not app.is_running
