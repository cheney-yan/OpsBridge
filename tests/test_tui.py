"""Smoke tests for the textual TUI App.

Full textual rendering tests need `pytest-textual-snapshot`; we keep these
lean: instantiate the App, hit a few public methods, exercise quit/slash.
Per-role widget rendering is covered by tests/test_phase3_batch_f.py
(PRD-phase3 §16 — Claude-Code-style stream UI).
"""
from __future__ import annotations

import pytest

from opsbridge.agent.tui import OpsBridgeApp


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
