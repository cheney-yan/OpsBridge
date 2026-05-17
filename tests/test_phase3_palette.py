"""Phase 3 follow-up: Subtle palette regression tests.

After the visual differentiation pass (user input / AI / bash cmd / bash
output / tool / system kinds), assert each write_top callsite tags the
line with the right `kind` and the resulting Rich markup is in the
captured string.
"""
from __future__ import annotations

import pytest

from opsbridge.agent.tui import OpsBridgeApp, _TOP_LOG_STYLES


def _capture_writes(app):
    written: list[tuple[str, str]] = []

    def capture(line: str, *, kind: str = "bash_out") -> None:
        written.append((kind, line))

    app.write_top = capture  # type: ignore[method-assign]
    return written


def test_palette_table_covers_all_kinds():
    assert set(_TOP_LOG_STYLES) == {
        "user", "ai", "bash_cmd", "bash_out", "tool", "system",
    }


def test_palette_user_has_background_tint():
    assert "on " in _TOP_LOG_STYLES["user"]


def test_palette_ai_has_background_tint():
    assert "on " in _TOP_LOG_STYLES["ai"]


def test_palette_bash_out_has_no_styling():
    assert _TOP_LOG_STYLES["bash_out"] == ""


def test_palette_bash_cmd_is_foreground_only():
    style = _TOP_LOG_STYLES["bash_cmd"]
    assert "on " not in style
    assert style != ""


def test_palette_tool_is_muted():
    assert _TOP_LOG_STYLES["tool"] == "dim"


def test_palette_system_is_warning_colored():
    assert "yellow" in _TOP_LOG_STYLES["system"]


@pytest.mark.asyncio
async def test_operator_input_tagged_user_kind():
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        written = _capture_writes(app)
        await pilot.press(*list("hello"), "enter")
        await pilot.pause()
    user_lines = [l for k, l in written if k == "user"]
    assert any("> hello" in l for l in user_lines)


@pytest.mark.asyncio
async def test_help_body_tagged_system_kind():
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        written = _capture_writes(app)
        await pilot.press(*list("/help"), "enter")
        await pilot.pause()
    kinds_seen = {k for k, _l in written}
    assert "user" in kinds_seen
    assert "system" in kinds_seen


@pytest.mark.asyncio
async def test_direct_bang_tagged_user_kind():
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
        on_direct_bash=lambda _c: None,
    )
    async with app.run_test() as pilot:
        written = _capture_writes(app)
        await pilot.press("!", *list("ls"), "enter")
        await pilot.pause()
    bang_lines = [l for k, l in written if k == "user"]
    assert any("! ls" in l for l in bang_lines)


@pytest.mark.asyncio
async def test_queue_full_message_tagged_system_kind():
    sent: list[str] = []
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda t: sent.append(t),
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        written = _capture_writes(app)
        for i in range(app.MAX_QUEUE_DEPTH):
            await pilot.press(*list(f"q{i}"), "enter")
            await pilot.pause()
        await pilot.press(*list("over"), "enter")
        await pilot.pause()
    system_lines = [l for k, l in written if k == "system"]
    assert any("queue full" in l for l in system_lines)


@pytest.mark.asyncio
async def test_brackets_in_user_input_dont_break_rendering():
    """Operator input legitimately containing brackets must survive
    Rich markup parsing (we escape on the way in).
    """
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.write_top("> ls [opt] foo", kind="user")
        await pilot.pause()
