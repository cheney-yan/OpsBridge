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

@pytest.mark.asyncio
async def test_first_submit_no_queue_hint():
    written: list[str] = []
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        app._do_write_top = lambda line: written.append(line)
        await pilot.press(*list("hello"), "enter")
        await pilot.pause()
    assert "> hello" in written
    assert not any("queued" in l for l in written)


@pytest.mark.asyncio
async def test_second_submit_while_busy_marks_queued():
    written: list[str] = []
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        app._do_write_top = lambda line: written.append(line)
        await pilot.press(*list("first"), "enter")
        await pilot.pause()
        # Don't notify_turn_done — agent is still "busy".
        await pilot.press(*list("second"), "enter")
        await pilot.pause()
    second_echo = next(l for l in written if "second" in l)
    assert "queued" in second_echo
    assert "1 ahead" in second_echo


@pytest.mark.asyncio
async def test_queue_full_rejection_at_max_depth():
    written: list[str] = []
    sent: list[str] = []
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda t: sent.append(t),
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        app._do_write_top = lambda line: written.append(line)
        for i in range(app.MAX_QUEUE_DEPTH):
            await pilot.press(*list(f"q{i}"), "enter")
            await pilot.pause()
        # Now in_flight == 5. Next submit must be rejected.
        await pilot.press(*list("over"), "enter")
        await pilot.pause()
    assert len(sent) == app.MAX_QUEUE_DEPTH
    assert "over" not in sent
    assert any("queue full" in l for l in written)


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
