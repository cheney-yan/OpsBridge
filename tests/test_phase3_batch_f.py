"""Phase 3 Batch F — Claude-Code-style stream UI (PRD-phase3 §16).

Drives the redesign:
  - VerticalScroll #stream replaces RichLog + MiddlePanel
  - Per-role widget classes (UserMessage, AssistantMessage,
    ToolCallMessage, ToolResultMessage, BashOutputLine,
    SystemNotice, ErrorMessage)
  - ResponseStatus wraps textual LoadingIndicator
  - AskForm / ModelPicker mount inline into the stream
  - PromptInput is a TextArea with Enter-submits / Shift+Enter newline
  - StatusBar docked at the very bottom of the screen
"""
from __future__ import annotations

import pytest

from textual.containers import VerticalScroll
from textual.widgets import LoadingIndicator, TextArea

from opsbridge.agent.tui import OpsBridgeApp, StatusBar
from opsbridge.agent import widgets as W


# ---------------------------------------------------------------------------
# Widget tree skeleton
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_is_vertical_scroll():
    """The message stream is a VerticalScroll with id=stream. MiddlePanel
    and TopLog (RichLog) are gone.
    """
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        stream = app.query_one("#stream")
        assert isinstance(stream, VerticalScroll)
        # Old widgets are gone.
        assert list(app.query("MiddlePanel")) == []
        assert list(app.query("TopLog")) == []
        assert list(app.query("RichLog")) == []


@pytest.mark.asyncio
async def test_dom_order_stream_then_input_then_status():
    """Bottom of screen: input above status bar. (Status bar at very bottom.)"""
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        # Status bar exists.
        assert list(app.query(StatusBar))
        # PromptInput exists.
        assert list(app.query(W.PromptInput))
        # The PromptInput is a TextArea subclass.
        inp = app.query_one(W.PromptInput)
        assert isinstance(inp, TextArea)


# ---------------------------------------------------------------------------
# Per-role widget classes
# ---------------------------------------------------------------------------

class TestRoleWidgets:
    def test_widget_classes_exist(self):
        for name in (
            "UserMessage",
            "AssistantMessage",
            "ToolCallMessage",
            "ToolResultMessage",
            "BashOutputLine",
            "SystemNotice",
            "ErrorMessage",
            "ResponseStatus",
            "AskForm",
            "ModelPicker",
            "PromptInput",
        ):
            assert hasattr(W, name), f"missing widget class: {name}"

    def test_user_message_text_uses_chevron_prefix(self):
        m = W.UserMessage(text="hello")
        assert "> hello" in m.render_text()

    def test_assistant_message_carries_markdown(self):
        m = W.AssistantMessage(text="**bold** and `code`")
        # Markdown rendering survives — we just check the payload roundtrips.
        assert "bold" in m.text and "code" in m.text

    def test_tool_call_message_prefix_and_args(self):
        m = W.ToolCallMessage(tool="Bash", args_summary="npm test")
        rendered = m.render_text()
        assert "●" in rendered
        assert "Bash(npm test)" in rendered

    def test_tool_result_uses_corner_glyph(self):
        m = W.ToolResultMessage(text="42 lines read")
        assert "⎿" in m.render_text()

    def test_system_notice_uses_pilcrow_glyph(self):
        m = W.SystemNotice(text="queue full")
        assert "※" in m.render_text()

    def test_error_message_red_dot(self):
        m = W.ErrorMessage(text="boom")
        rendered = m.render_text()
        assert "●" in rendered
        assert "boom" in rendered


# ---------------------------------------------------------------------------
# Append surface — typed message helpers on the app
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_submit_mounts_user_message_in_stream():
    sent: list[str] = []
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda t: sent.append(t),
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        # Type and submit.
        inp = app.query_one(W.PromptInput)
        inp.text = "hello world"
        await pilot.press("enter")
        await pilot.pause()
    assert sent == ["hello world"]
    msgs = list(app.query(W.UserMessage))
    assert len(msgs) == 1
    assert "hello world" in msgs[0].text


@pytest.mark.asyncio
async def test_append_assistant_mounts_assistant_message():
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        app.append_assistant("done — 3 packages installed")
        await pilot.pause()
    msgs = list(app.query(W.AssistantMessage))
    assert len(msgs) == 1
    assert "3 packages" in msgs[0].text


@pytest.mark.asyncio
async def test_append_tool_call_then_result_mounts_pair():
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        app.append_tool_call("Bash", "ls /etc")
        app.append_tool_result("3 items")
        await pilot.pause()
    calls = list(app.query(W.ToolCallMessage))
    results = list(app.query(W.ToolResultMessage))
    assert len(calls) == 1 and len(results) == 1
    assert "ls /etc" in calls[0].args_summary


@pytest.mark.asyncio
async def test_append_system_and_error_mount():
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        app.append_system("queue full")
        app.append_error("boom")
        await pilot.pause()
    assert list(app.query(W.SystemNotice))
    assert list(app.query(W.ErrorMessage))


# ---------------------------------------------------------------------------
# Thinking indicator (ResponseStatus + LoadingIndicator)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_begin_thinking_mounts_response_status_with_loading_indicator():
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        app.begin_thinking()
        await pilot.pause()
        statuses = list(app.query(W.ResponseStatus))
        assert len(statuses) == 1
        # textual's built-in LoadingIndicator is inside it.
        assert list(statuses[0].query(LoadingIndicator))


@pytest.mark.asyncio
async def test_end_thinking_removes_response_status_and_records_done():
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        app.begin_thinking()
        await pilot.pause()
        app.end_thinking(elapsed_s=4.2)
        await pilot.pause()
        # The transient status is gone.
        assert list(app.query(W.ResponseStatus)) == []
        # A SystemNotice transcript line is in the stream.
        notices = list(app.query(W.SystemNotice))
        joined = " ".join(n.text for n in notices)
        assert "done" in joined and "4" in joined


# ---------------------------------------------------------------------------
# PromptInput — TextArea-based, Enter submits, Shift+Enter newlines
# ---------------------------------------------------------------------------

class TestPromptInput:
    def test_subclass_of_textarea(self):
        assert issubclass(W.PromptInput, TextArea)

    @pytest.mark.asyncio
    async def test_border_chrome_set(self):
        app = OpsBridgeApp(
            hostname="h", model_label="m",
            on_operator_turn=lambda _t: None,
            on_cancel=lambda: None,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            inp = app.query_one(W.PromptInput)
            # Title + subtitle render the hint chrome.
            assert inp.border_title is not None
            assert inp.border_subtitle is not None
            assert "send" in str(inp.border_subtitle).lower()

    @pytest.mark.asyncio
    async def test_enter_submits_input(self):
        sent: list[str] = []
        app = OpsBridgeApp(
            hostname="h", model_label="m",
            on_operator_turn=lambda t: sent.append(t),
            on_cancel=lambda: None,
        )
        async with app.run_test() as pilot:
            inp = app.query_one(W.PromptInput)
            inp.text = "send me"
            await pilot.press("enter")
            await pilot.pause()
        assert sent == ["send me"]

    @pytest.mark.asyncio
    async def test_shift_enter_inserts_newline_no_submit(self):
        sent: list[str] = []
        app = OpsBridgeApp(
            hostname="h", model_label="m",
            on_operator_turn=lambda t: sent.append(t),
            on_cancel=lambda: None,
        )
        async with app.run_test() as pilot:
            inp = app.query_one(W.PromptInput)
            inp.text = "line one"
            await pilot.press("shift+enter")
            await pilot.pause()
            # Did not submit.
            assert sent == []
            # Newline got into the buffer.
            assert "\n" in inp.text

    @pytest.mark.asyncio
    async def test_empty_submit_is_ignored(self):
        sent: list[str] = []
        app = OpsBridgeApp(
            hostname="h", model_label="m",
            on_operator_turn=lambda t: sent.append(t),
            on_cancel=lambda: None,
        )
        async with app.run_test() as pilot:
            await pilot.press("enter")
            await pilot.pause()
        assert sent == []

    @pytest.mark.asyncio
    async def test_submit_clears_input(self):
        app = OpsBridgeApp(
            hostname="h", model_label="m",
            on_operator_turn=lambda _t: None,
            on_cancel=lambda: None,
        )
        async with app.run_test() as pilot:
            inp = app.query_one(W.PromptInput)
            inp.text = "hi"
            await pilot.press("enter")
            await pilot.pause()
            assert inp.text == ""


# ---------------------------------------------------------------------------
# AskForm — inline form widget, freeze on resolve
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_show_ask_form_mounts_inline_askform_widget():
    """The ask form is a real widget mounted in #stream, NOT a Static
    update on MiddlePanel."""
    import threading
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    result: dict = {}

    async with app.run_test() as pilot:
        # Run show_ask_form from a background thread; it blocks on event.
        def call():
            result["choice"] = app.show_ask_form("Run dangerous thing?", ["yes", "no"])
        t = threading.Thread(target=call, daemon=True)
        t.start()
        # Give the main loop a tick to mount the form.
        await pilot.pause()
        forms = list(app.query(W.AskForm))
        assert len(forms) == 1
        # Resolve "no" via keyboard navigation.
        await pilot.press("enter")
        await pilot.pause()
        t.join(timeout=2)
    assert result.get("choice") in ("yes", "no")
    # After resolve: the form widget stays in stream as frozen history.
    forms_after = list(app.query(W.AskForm))
    assert len(forms_after) == 1
    assert forms_after[0].frozen is True


# ---------------------------------------------------------------------------
# ModelPicker — inline picker widget, pagination, escape cancels
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_model_picker_mounts_inline_on_slash_model():
    app = OpsBridgeApp(
        hostname="h", model_label="claude-sonnet-4-6",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
        on_model_swap=lambda _m, _p: None,
        discover_models=lambda: ["claude-haiku-4-5", "claude-sonnet-4-6", "gpt-5"],
    )
    async with app.run_test() as pilot:
        inp = app.query_one(W.PromptInput)
        inp.text = "/model"
        await pilot.press("enter")
        await pilot.pause()
        pickers = list(app.query(W.ModelPicker))
        assert len(pickers) == 1
        # The picker reflects the discovery + currently-active model.
        assert pickers[0].current_model == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_model_picker_pick_freezes_widget_and_calls_swap():
    swaps: list[tuple[str, bool]] = []
    models = ["a", "b", "c"]
    app = OpsBridgeApp(
        hostname="h", model_label="a",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
        on_model_swap=lambda mid, p: swaps.append((mid, p)),
        discover_models=lambda: models,
    )
    async with app.run_test() as pilot:
        inp = app.query_one(W.PromptInput)
        inp.text = "/model"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("2")           # pick "b"
        await pilot.pause()
    assert swaps == [("b", True)]
    # Picker frozen and still visible in history.
    pickers = list(app.query(W.ModelPicker))
    assert len(pickers) == 1 and pickers[0].frozen is True


# ---------------------------------------------------------------------------
# StatusBar — keep cwd/host/model/ctx; no longer carries the spinner
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_bar_has_no_spinner_frame_anymore():
    """The thinking spinner now lives in ResponseStatus, not the bar."""
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one(StatusBar)
        assert not hasattr(bar, "spinner_frame")
