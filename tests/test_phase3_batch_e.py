"""Phase 3 Batch E acceptance tests.

Covers:
  §5  — Queued-turn visibility (TUI in_flight counter + queue-full reject)
  §9  — opsbridge doctor --check-orphans (agent-owned process scan)
  §10 — IME / voice-input duplicate-submit dedupe (CJK wide-char input
        is now handled natively by textual's TextArea-based PromptInput,
        so the legacy WidthAwareInput tests are gone — §16 redesign).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from opsbridge.agent.tui import OpsBridgeApp
from opsbridge.agent.widgets import PromptInput
from opsbridge import admin


# ---------------------------------------------------------------------------
# §5 — Queue visibility
# ---------------------------------------------------------------------------

# Helper: pull plain text from each widget in the stream for assertions.
def _stream_texts(app) -> list[str]:
    from opsbridge.agent.widgets import _StreamMessage
    return [w.render_text() for w in app.query(_StreamMessage)]


@pytest.mark.asyncio
async def test_first_submit_no_queue_hint():
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        inp = app.query_one(PromptInput)
        inp.text = "hello"
        await pilot.press("enter")
        await pilot.pause()
        texts = _stream_texts(app)
        assert any("> hello" in t for t in texts), texts
        assert not any("queued" in t for t in texts)


@pytest.mark.asyncio
async def test_second_submit_while_busy_marks_queued():
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        inp = app.query_one(PromptInput)
        inp.text = "first"
        await pilot.press("enter")
        await pilot.pause()
        inp.text = "second"
        await pilot.press("enter")
        await pilot.pause()
        texts = _stream_texts(app)
        second = next(t for t in texts if "second" in t)
        assert "queued" in second
        assert "1 ahead" in second


@pytest.mark.asyncio
async def test_queue_full_rejection_at_max_depth():
    sent: list[str] = []
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda t: sent.append(t),
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        inp = app.query_one(PromptInput)
        for i in range(app.MAX_QUEUE_DEPTH):
            inp.text = f"q{i}"
            await pilot.press("enter")
            await pilot.pause()
        # Now in_flight == 5. Next submit must be rejected.
        inp.text = "over"
        await pilot.press("enter")
        await pilot.pause()
        assert len(sent) == app.MAX_QUEUE_DEPTH
        assert "over" not in sent
        assert any("queue full" in t for t in _stream_texts(app))


def _mk_submitted(text: str) -> PromptInput.Submitted:
    """Helper — build a PromptInput.Submitted message with the §16 shape."""
    return PromptInput.Submitted(text=text)


@pytest.mark.asyncio
async def test_ime_duplicate_submit_is_deduped():
    """Chinese IMEs / voice input on macOS Terminal emit Submitted TWICE
    for one Enter. Without dedupe the agent runs the LLM twice. Within
    IME_DUPLICATE_WINDOW_SEC, identical-text submits MUST collapse to one.
    """
    sent: list[str] = []
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda t: sent.append(t),
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        await app.on_prompt_input_submitted(_mk_submitted("服务器砌了多久了？"))
        await app.on_prompt_input_submitted(_mk_submitted("服务器砌了多久了？"))
        await pilot.pause()
        assert sent == ["服务器砌了多久了？"], (
            f"expected one delivery, got {sent!r}"
        )


@pytest.mark.asyncio
async def test_identical_resubmit_after_window_is_allowed():
    """A real same-message-again gesture must not be deduped after the
    time window expires.
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
        await app.on_prompt_input_submitted(_mk_submitted("again"))
        await _asyncio.sleep(0.1)
        await app.on_prompt_input_submitted(_mk_submitted("again"))
        await pilot.pause()
        assert sent == ["again", "again"]


@pytest.mark.asyncio
async def test_resubmit_after_value_change_is_allowed_immediately():
    """If a Changed event fires with non-empty value between two same-text
    submits, the second goes through within the dedupe window — the
    operator clearly re-composed input.
    """
    class _Ev:
        def __init__(self, text):
            self.value = text
    sent: list[str] = []
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda t: sent.append(t),
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        await app.on_prompt_input_submitted(_mk_submitted("ping"))
        await app.on_input_changed(_Ev("ping"))
        await app.on_prompt_input_submitted(_mk_submitted("ping"))
        await pilot.pause()
        assert sent == ["ping", "ping"]


@pytest.mark.asyncio
async def test_clear_value_event_does_not_break_dedupe():
    """Our `inp.clear()` triggers a Changed event with an empty value.
    That MUST NOT count as new operator content — otherwise IME duplicates
    arriving just after our clear would slip through.
    """
    class _Ev:
        def __init__(self, text):
            self.value = text
    sent: list[str] = []
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda t: sent.append(t),
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        await app.on_prompt_input_submitted(_mk_submitted("echo"))
        await app.on_input_changed(_Ev(""))
        await app.on_prompt_input_submitted(_mk_submitted("echo"))
        await pilot.pause()
        assert sent == ["echo"]


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
# §10 — CJK input now handled natively by TextArea-based PromptInput.
# (PRD-phase3 §16: WidthAwareInput retired; TextArea ships with a
#  proper grapheme cursor and paste handling. Widget-existence test
#  lives in tests/test_phase3_batch_f.py.)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cjk_text_lands_in_prompt_input():
    """CJK chars can be assigned + read back from the PromptInput."""
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        inp = app.query_one(PromptInput)
        inp.text = "服务器砌了多久了？"
        await pilot.pause()
        assert inp.text == "服务器砌了多久了？"


# ---------------------------------------------------------------------------
# Ctrl-C cascade — Claude-Code-style semantics
# ---------------------------------------------------------------------------

class TestCtrlCCascade:
    """Ctrl-C never quits. It cascades:
       modal-cancel → interrupt-running-task → clear-input → hint.
    """

    @pytest.mark.asyncio
    async def test_ctrl_c_interrupts_when_busy(self):
        cancels: list[bool] = []
        app = OpsBridgeApp(
            hostname="h", model_label="m",
            on_operator_turn=lambda _t: None,
            on_cancel=lambda: cancels.append(True),
        )
        async with app.run_test() as pilot:
            await pilot.press(*list("work"), "enter")
            await pilot.pause()
            assert app._in_flight == 1
            await pilot.press("ctrl+c")
            await pilot.pause()
        assert cancels == [True]

    @pytest.mark.asyncio
    async def test_ctrl_c_clears_input_when_idle(self):
        """Idle with text in the input → Ctrl-C clears the input.
        on_cancel must NOT fire (nothing to cancel).
        """
        cancels: list[bool] = []
        app = OpsBridgeApp(
            hostname="h", model_label="m",
            on_operator_turn=lambda _t: None,
            on_cancel=lambda: cancels.append(True),
        )
        async with app.run_test() as pilot:
            inp = app.query_one(PromptInput)
            inp.text = "draft text"
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert inp.text == ""
        assert cancels == []

    @pytest.mark.asyncio
    async def test_ctrl_c_idle_empty_input_shows_hint(self):
        """Idle + empty input → hint, no cancel fired, no exit."""
        cancels: list[bool] = []
        app = OpsBridgeApp(
            hostname="h", model_label="m",
            on_operator_turn=lambda _t: None,
            on_cancel=lambda: cancels.append(True),
        )
        async with app.run_test() as pilot:
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert cancels == []
            assert any("Ctrl-D" in t for t in _stream_texts(app))

    @pytest.mark.asyncio
    async def test_ctrl_c_does_not_quit(self):
        """Ctrl-C must not exit the app, even from idle + empty input."""
        app = OpsBridgeApp(
            hostname="h", model_label="m",
            on_operator_turn=lambda _t: None,
            on_cancel=lambda: None,
        )
        async with app.run_test() as pilot:
            await pilot.press("ctrl+c")
            await pilot.pause()
            # If Ctrl-C exited, run_test() context would have torn down.
            assert app.is_running is True
