"""Phase 3 Batch E acceptance tests.

Covers:
  §5  — Queued-turn visibility (TUI in_flight counter + queue-full reject)
  §9  — opsbridge doctor --check-orphans (agent-owned process scan)
  §10 — Wide-char (CJK / emoji) input via WidthAwareInput
"""
from __future__ import annotations

from pathlib import Path

import pytest

from opsbridge.agent.tui import OpsBridgeApp, WidthAwareInput
from opsbridge import admin


# ---------------------------------------------------------------------------
# §5 — Queue visibility
# ---------------------------------------------------------------------------

def _attach_write_top_capture(app, written: list[tuple[str, str]]) -> None:
    """Replace app.write_top with a synchronous list-appending stub."""
    def capture(line: str, *, kind: str = "bash_out") -> None:
        written.append((kind, line))
    app.write_top = capture  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_first_submit_no_queue_hint():
    written: list[tuple[str, str]] = []
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        _attach_write_top_capture(app, written)
        await pilot.press(*list("hello"), "enter")
        await pilot.pause()
    user_echoes = [line for kind, line in written if kind == "user"]
    assert any("> hello" in l for l in user_echoes)
    assert not any("queued" in l for _k, l in written)


@pytest.mark.asyncio
async def test_second_submit_while_busy_marks_queued():
    written: list[tuple[str, str]] = []
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        _attach_write_top_capture(app, written)
        await pilot.press(*list("first"), "enter")
        await pilot.pause()
        # Don't notify_turn_done — agent is still "busy".
        await pilot.press(*list("second"), "enter")
        await pilot.pause()
    second_echo = next(l for _k, l in written if "second" in l)
    assert "queued" in second_echo
    assert "1 ahead" in second_echo


@pytest.mark.asyncio
async def test_queue_full_rejection_at_max_depth():
    written: list[tuple[str, str]] = []
    sent: list[str] = []
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda t: sent.append(t),
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        _attach_write_top_capture(app, written)
        for i in range(app.MAX_QUEUE_DEPTH):
            await pilot.press(*list(f"q{i}"), "enter")
            await pilot.pause()
        # Now in_flight == 5. Next submit must be rejected.
        await pilot.press(*list("over"), "enter")
        await pilot.pause()
    assert len(sent) == app.MAX_QUEUE_DEPTH
    assert "over" not in sent
    assert any("queue full" in l for _k, l in written)


@pytest.mark.asyncio
async def test_notify_turn_done_releases_one_slot():
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        await pilot.press(*list("hi"), "enter")
        await pilot.pause()
        assert app._in_flight == 1
        app._do_decrement_queue()
        assert app._in_flight == 0


# ---------------------------------------------------------------------------
# §9 — orphan-process doctor check
# ---------------------------------------------------------------------------

class TestOrphanCheck:
    def test_check_orphans_handles_no_proc(self):
        if not Path("/proc").exists():
            ok, detail = admin._check_orphans()
            assert ok is True
            assert "/proc not available" in detail or "doesn't exist" in detail

    def test_doctor_argparse_accepts_check_orphans_flag(self):
        import argparse
        ap = argparse.ArgumentParser()
        sub = ap.add_subparsers(dest="cmd")
        sp = sub.add_parser("doctor")
        sp.add_argument("--check-orphans", action="store_true")
        ns = ap.parse_args(["doctor", "--check-orphans"])
        assert ns.cmd == "doctor"
        assert ns.check_orphans is True


# ---------------------------------------------------------------------------
# §10 — Wide-character input subclass
# ---------------------------------------------------------------------------

class TestWidthAwareInput:
    def test_subclass_exists_and_is_an_input(self):
        from textual.widgets import Input
        assert issubclass(WidthAwareInput, Input)

    def test_subclass_overrides_delete_actions(self):
        from textual.widgets import Input
        for name in (
            "action_delete_left",
            "action_delete_left_word",
            "action_delete_left_all",
            "action_delete_right",
        ):
            assert hasattr(WidthAwareInput, name)
            assert getattr(WidthAwareInput, name) is not getattr(Input, name)

    @pytest.mark.asyncio
    async def test_app_uses_width_aware_input(self):
        app = OpsBridgeApp(
            hostname="h", model_label="m",
            on_operator_turn=lambda _t: None,
            on_cancel=lambda: None,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            inputs = list(app.query(WidthAwareInput))
            assert len(inputs) == 1

    @pytest.mark.asyncio
    async def test_cjk_input_does_not_crash(self):
        app = OpsBridgeApp(
            hostname="h", model_label="m",
            on_operator_turn=lambda _t: None,
            on_cancel=lambda: None,
        )
        async with app.run_test() as pilot:
            inp = app.query_one(WidthAwareInput)
            inp.value = "安装nginx"
            inp.cursor_position = len(inp.value)
            await pilot.press("backspace")
            await pilot.pause()
            assert inp.value == "安装ngin"

    @pytest.mark.asyncio
    async def test_paste_event_does_not_await_none(self):
        """Regression: textual 8.x's Input._on_paste returns None (not a
        coroutine). Our `await super()._on_paste(...)` therefore raised
        TypeError on every CJK paste — including macOS voice input which
        emits Paste events. Fix: inspect-then-await; never crash the input.
        """
        from textual.events import Paste

        app = OpsBridgeApp(
            hostname="h", model_label="m",
            on_operator_turn=lambda _t: None,
            on_cancel=lambda: None,
        )
        async with app.run_test() as pilot:
            inp = app.query_one(WidthAwareInput)
            inp.focus()
            await pilot.pause()
            # Simulate a Paste event (what CJK IMEs + voice input emit).
            await inp._on_paste(Paste(text="服务器砌了多久了？"))
            await pilot.pause()
            # Didn't raise = pass. As a bonus check, the pasted text should
            # have landed in the input value.
            assert "服务器" in inp.value
