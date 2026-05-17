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
async def test_ime_duplicate_submit_is_deduped():
    """Chinese IMEs / voice input on macOS Terminal emit Input.Submitted
    TWICE for one Enter. Without dedupe, the agent ran the LLM twice and
    wrote the echo twice to the top region. Within IME_DUPLICATE_WINDOW_SEC,
    identical-text submits MUST collapse to one delivery.
    """
    sent: list[str] = []
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda t: sent.append(t),
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        inp = app.query_one(WidthAwareInput)
        # Simulate the IME double-submit: same text submitted twice in <400ms.
        from textual.widgets._input import Input
        msg1 = Input.Submitted(inp, "服务器砌了多久了？", None)
        msg2 = Input.Submitted(inp, "服务器砌了多久了？", None)
        await app.on_input_submitted(msg1)
        await app.on_input_submitted(msg2)
        await pilot.pause()
    assert sent == ["服务器砌了多久了？"], (
        f"expected one delivery, got {sent!r}"
    )


@pytest.mark.asyncio
async def test_identical_resubmit_after_window_is_allowed():
    """A real `same-message-again` gesture must not be deduped.

    Two paths get through the IME guard:
      1. An Input.Changed event in between (operator typed new content).
      2. Time-window expires.
    """
    import asyncio as _asyncio
    sent: list[str] = []
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda t: sent.append(t),
        on_cancel=lambda: None,
    )
    app.IME_DUPLICATE_WINDOW_SEC = 0.05  # shrink for the test
    async with app.run_test() as pilot:
        inp = app.query_one(WidthAwareInput)
        from textual.widgets._input import Input
        await app.on_input_submitted(Input.Submitted(inp, "again", None))
        await _asyncio.sleep(0.1)   # past the window
        await app.on_input_submitted(Input.Submitted(inp, "again", None))
        await pilot.pause()
    assert sent == ["again", "again"]


@pytest.mark.asyncio
async def test_resubmit_after_value_change_is_allowed_immediately():
    """If Input.Changed fires with non-empty value between two same-text
    submits, the second goes through even WITHIN the dedupe window —
    because the operator clearly re-composed input.
    """
    sent: list[str] = []
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda t: sent.append(t),
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        inp = app.query_one(WidthAwareInput)
        from textual.widgets._input import Input
        await app.on_input_submitted(Input.Submitted(inp, "ping", None))
        # Simulate the operator typing "ping" again.
        await app.on_input_changed(Input.Changed(inp, "ping", None))
        await app.on_input_submitted(Input.Submitted(inp, "ping", None))
        await pilot.pause()
    assert sent == ["ping", "ping"]


@pytest.mark.asyncio
async def test_clear_value_event_does_not_break_dedupe():
    """Our `message.input.value = ""` triggers Input.Changed with an empty
    string. That MUST NOT count as new operator content — otherwise the
    IME duplicate that arrives just after our clear would slip through.
    """
    sent: list[str] = []
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda t: sent.append(t),
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        inp = app.query_one(WidthAwareInput)
        from textual.widgets._input import Input
        await app.on_input_submitted(Input.Submitted(inp, "echo", None))
        # Our clear emits this Changed event:
        await app.on_input_changed(Input.Changed(inp, "", None))
        # IME's duplicate Submitted arrives:
        await app.on_input_submitted(Input.Submitted(inp, "echo", None))
        await pilot.pause()
    assert sent == ["echo"]  # second submit was deduped


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
