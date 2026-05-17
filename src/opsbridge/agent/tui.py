"""Textual TUI for OpsBridge agent — four-region layout.

PRD-phase2.md §"Region layout":
  header (1 row) · top scrollable output (flex) · middle final-answer
  or form (2–10 rows) · status bar (1 row) · input line (1 row).

Threading model: the agent runs on a background daemon thread fed by a
queue; rendering and operator input live on textual's asyncio event
loop. Agent → App messages cross threads via `App.call_from_thread`.
"""
from __future__ import annotations

import asyncio
import queue
import threading
from dataclasses import dataclass, field
from typing import Callable

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import Footer, Header, Input, RichLog, Static


# ---------------------------------------------------------------------------
# Inter-thread messages
# ---------------------------------------------------------------------------

@dataclass
class _AskState:
    prompt: str
    options: list[str]
    event: threading.Event
    chosen: dict
    selected_idx: int = 0


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class StatusBar(Static):
    """One-row status indicator."""

    state = reactive("idle")
    detail = reactive("")
    spinner_frame = reactive(0)

    SPINNER = "◐◓◑◒"

    def render(self) -> str:  # type: ignore[override]
        glyph = self.SPINNER[self.spinner_frame % len(self.SPINNER)]
        s = self.state
        d = f" · {self.detail}" if self.detail else ""
        if s == "idle":
            return f"  idle{d}"
        return f"  {glyph} {s}{d}"


class TopLog(RichLog):
    """Scrollable top region — bash output, search/visit chatter, etc."""


class MiddlePanel(Static):
    """Middle region — either the LLM's final answer for the current turn
    or, when an ask is active, a form.
    """

    def show_text(self, text: str) -> None:
        self.update(text or "")

    def show_form(self, state: _AskState) -> None:
        self.update(_render_form(state))


def _render_form(state: _AskState) -> str:
    lines = ["▶ " + state.prompt, ""]
    for i, opt in enumerate(state.options):
        marker = "•" if i == state.selected_idx else " "
        default = " ← default" if i == 0 and i != state.selected_idx else ""
        lines.append(f"  ({marker}) {opt}{default}")
    lines.append("")
    lines.append("[arrows/Y/N to select, Enter to confirm, Ctrl-C to cancel]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class OpsBridgeApp(App):
    """Four-region TUI."""

    CSS = """
    Screen { layout: vertical; }
    #top_log { height: 1fr; border: solid $primary; padding: 0 1; }
    #middle { min-height: 2; max-height: 12; border: solid $accent; padding: 0 1; background: $boost; }
    StatusBar { height: 1; background: $surface; color: $text; }
    Input { dock: bottom; }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel", "Cancel", priority=True),
        # priority=True so the Input widget doesn't swallow Ctrl-D as
        # "delete forward char" — we want it to always reach action_quit.
        Binding("ctrl+d", "quit", "Quit (×2)", priority=True),
    ]

    # Double-press window for Ctrl-D quit (seconds). First press arms,
    # second press within the window actually exits. Prevents accidental
    # disconnects mid-task — Ctrl-D is a common reflex when the operator
    # *meant* to clear the input line.
    QUIT_ARM_WINDOW_SEC = 2.0

    def __init__(
        self,
        *,
        hostname: str,
        model_label: str,
        on_operator_turn: Callable[[str], None],
        on_cancel: Callable[[], None],
        title: str = "OpsBridge",
    ) -> None:
        super().__init__()
        self._hostname = hostname
        self._model_label = model_label
        self._on_operator_turn = on_operator_turn
        self._on_cancel = on_cancel
        self.title = f"{title} · {hostname} · {model_label}"
        self._active_ask: _AskState | None = None
        self._ctx_pct = 0
        self._spinner_task: asyncio.Task | None = None
        self._quit_armed_at: float | None = None

    # ----- composition ----------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            yield TopLog(id="top_log", wrap=True, highlight=False, markup=False)
            yield MiddlePanel(id="middle")
            yield StatusBar(id="status")
        yield Input(id="prompt", placeholder="ask the agent…")

    async def on_mount(self) -> None:
        self.query_one(Input).focus()
        self._spinner_task = asyncio.create_task(self._tick_spinner())

    async def on_unmount(self) -> None:
        if self._spinner_task is not None:
            self._spinner_task.cancel()

    async def _tick_spinner(self) -> None:
        try:
            while True:
                bar = self.query_one(StatusBar)
                if bar.state != "idle":
                    bar.spinner_frame = bar.spinner_frame + 1
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            pass

    # ----- public methods (called from agent thread via call_from_thread) -

    def write_top(self, line: str) -> None:
        """Append a line to the top region. Safe from any thread."""
        try:
            self.call_from_thread(self._do_write_top, line)
        except RuntimeError:
            # App not started yet — just buffer to stdout.
            print(line)

    def _do_write_top(self, line: str) -> None:
        try:
            log = self.query_one(TopLog)
            log.write(line)
        except Exception:  # noqa: BLE001
            pass

    def set_status(self, state: str, detail: str = "") -> None:
        try:
            self.call_from_thread(self._do_set_status, state, detail)
        except RuntimeError:
            pass

    def _do_set_status(self, state: str, detail: str) -> None:
        try:
            bar = self.query_one(StatusBar)
            bar.state = state
            bar.detail = detail
        except Exception:  # noqa: BLE001
            pass

    def set_final_answer(self, text: str) -> None:
        try:
            self.call_from_thread(self._do_set_final_answer, text)
        except RuntimeError:
            pass

    def _do_set_final_answer(self, text: str) -> None:
        try:
            mid = self.query_one(MiddlePanel)
            mid.show_text(text)
        except Exception:  # noqa: BLE001
            pass

    def set_context_percent(self, pct: int) -> None:
        self._ctx_pct = pct
        try:
            self.call_from_thread(self._do_update_subtitle)
        except RuntimeError:
            pass

    def _do_update_subtitle(self) -> None:
        self.sub_title = f"ctx {self._ctx_pct}%"

    # ----- ask form -------------------------------------------------------

    def show_ask_form(self, prompt: str, options: list[str]) -> str:
        """Block the calling (agent) thread until the operator answers.

        Must NOT be called from the main thread.
        """
        event = threading.Event()
        slot: dict = {"choice": None}
        state = _AskState(prompt=prompt, options=list(options), event=event, chosen=slot)
        try:
            self.call_from_thread(self._activate_ask_form, state)
        except RuntimeError:
            # No event loop running — fall back to default option.
            return options[0]
        event.wait()
        return slot["choice"] or options[0]

    def _activate_ask_form(self, state: _AskState) -> None:
        self._active_ask = state
        self._do_set_status("awaiting input", "")
        try:
            mid = self.query_one(MiddlePanel)
            mid.show_form(state)
        except Exception:  # noqa: BLE001
            pass

    def _resolve_ask(self, choice: str) -> None:
        if self._active_ask is None:
            return
        state = self._active_ask
        self._active_ask = None
        state.chosen["choice"] = choice
        state.event.set()
        self._do_set_status("idle", "")
        try:
            mid = self.query_one(MiddlePanel)
            mid.show_text("")
        except Exception:  # noqa: BLE001
            pass

    # ----- key handling for the ask form ----------------------------------

    async def on_key(self, event) -> None:
        if self._active_ask is None:
            return
        state = self._active_ask
        key = event.key
        if key in ("up", "left"):
            state.selected_idx = (state.selected_idx - 1) % len(state.options)
            self._refresh_form()
            event.stop()
            return
        if key in ("down", "right"):
            state.selected_idx = (state.selected_idx + 1) % len(state.options)
            self._refresh_form()
            event.stop()
            return
        if key.lower() in ("y",) and any(o.lower().startswith("y") for o in state.options):
            for i, o in enumerate(state.options):
                if o.lower().startswith("y"):
                    state.selected_idx = i
                    break
            self._refresh_form()
            event.stop()
            return
        if key.lower() in ("n",) and any(o.lower().startswith("n") for o in state.options):
            for i, o in enumerate(state.options):
                if o.lower().startswith("n"):
                    state.selected_idx = i
                    break
            self._refresh_form()
            event.stop()
            return
        if key == "enter":
            choice = state.options[state.selected_idx]
            self._resolve_ask(choice)
            event.stop()
            return

    def _refresh_form(self) -> None:
        if self._active_ask is None:
            return
        try:
            mid = self.query_one(MiddlePanel)
            mid.show_form(self._active_ask)
        except Exception:  # noqa: BLE001
            pass

    # ----- input ----------------------------------------------------------

    async def on_input_submitted(self, message: Input.Submitted) -> None:
        text = message.value.strip()
        message.input.value = ""
        if not text:
            return
        # Any keystroke past the arm window disarms quit — and typing through
        # the input is the clearest "I changed my mind" signal.
        self._quit_armed_at = None
        # Inline slash commands handled in-process so they don't waste an
        # LLM turn. `/quit` and `/exit` are the cleanest way out of the TUI.
        if text in ("/quit", "/exit", "/q"):
            self.exit()
            return
        # Echo into the top log.
        self._do_write_top(f"> {text}")
        # Clear last final-answer so the form/answer area shows what's current.
        self._do_set_final_answer("")
        # Hand to the agent thread.
        self._do_set_status("thinking", "")
        self._on_operator_turn(text)

    async def action_cancel(self) -> None:
        if self._active_ask is not None:
            self._resolve_ask("__cancelled__")
            return
        self._on_cancel()
        self._do_set_status("idle", "cancelled")

    async def action_quit(self) -> None:
        """Ctrl-D handler with a two-press confirmation.

        First press arms; status bar shows the hint. Second press within
        QUIT_ARM_WINDOW_SEC exits cleanly. Anything else (typing into the
        input, hitting Ctrl-C) disarms.
        """
        import time as _t
        now = _t.monotonic()
        if self._quit_armed_at is not None and (now - self._quit_armed_at) <= self.QUIT_ARM_WINDOW_SEC:
            self.exit()
            return
        self._quit_armed_at = now
        self._do_set_status(
            "awaiting input",
            f"press Ctrl-D again within {int(self.QUIT_ARM_WINDOW_SEC)}s to quit · or type /quit",
        )
