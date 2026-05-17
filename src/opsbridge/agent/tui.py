"""Textual TUI for OpsBridge agent — Claude-Code-style stream UI.

PRD-phase3 §16 layout (replaces §15 four-region design):

    VerticalScroll #stream  (1fr — fills)
      ├── UserMessage / ToolCallMessage / ToolResultMessage /
      ├── BashOutputLine / AssistantMessage / SystemNotice /
      ├── ErrorMessage / AskForm / ModelPicker / ResponseStatus
      └── … append-only, scroll-to-end
    PromptInput #prompt   (TextArea — rounded border, multi-line)
    StatusBar #status     (1 row — $primary bg, very bottom)

Role distinction is by prefix glyph + foreground color (no region bg
tints, no chrome borders between widgets). Inline forms (AskForm /
ModelPicker) take focus while live; on resolve they freeze in place
as history (audit-log "form-rendered" invariant preserved — CLAUDE.md).

Threading model: agent runs on a background daemon thread; rendering
and operator input live on textual's asyncio loop. Cross-thread
communication via `App.call_from_thread`.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import Static

from opsbridge.agent.widgets import (
    AskForm,
    AssistantMessage,
    BashOutputLine,
    ErrorMessage,
    ModelPicker,
    PromptInput,
    ResponseStatus,
    SystemNotice,
    ToolCallMessage,
    ToolResultMessage,
    UserMessage,
)


# ---------------------------------------------------------------------------
# Dataclasses — preserved for tests that import them
# ---------------------------------------------------------------------------

@dataclass
class _AskState:
    prompt: str
    options: list[str]
    event: threading.Event
    chosen: dict
    selected_idx: int = 0


@dataclass
class _PickerState:
    """Pure pagination math — used by TestPickerState in batch B.

    The live picker is the `ModelPicker` widget; this dataclass just
    exercises the math in unit tests.
    """
    models: list[str]
    selected_idx: int = 0
    page: int = 0
    page_size: int = 10
    current_model: str = ""

    @property
    def total_pages(self) -> int:
        return max(1, (len(self.models) + self.page_size - 1) // self.page_size)

    def page_slice(self) -> tuple[int, int]:
        start = self.page * self.page_size
        end = min(start + self.page_size, len(self.models))
        return start, end

    def visible(self) -> list[tuple[int, str]]:
        start, end = self.page_slice()
        return [(i, self.models[i]) for i in range(start, end)]

    def select_relative(self, delta: int) -> None:
        n = len(self.models)
        if n == 0:
            return
        self.selected_idx = (self.selected_idx + delta) % n
        self.page = self.selected_idx // self.page_size

    def page_relative(self, delta: int) -> None:
        new_page = max(0, min(self.total_pages - 1, self.page + delta))
        if new_page != self.page:
            self.page = new_page
            self.selected_idx = self.page * self.page_size


# ---------------------------------------------------------------------------
# StatusBar — single-row consolidated status (§15)
# ---------------------------------------------------------------------------

class StatusBar(Static):
    """One-row status: state · elapsed · cwd · @host · model · ctx%.

    Spinner glyph is omitted in §16 (the LoadingIndicator in
    ResponseStatus carries the "alive" signal). Fields drop right-to-left
    under width pressure.
    """

    state: reactive[str] = reactive("idle")
    elapsed: reactive[float] = reactive(0.0)
    cwd: reactive[str] = reactive("~")
    hostname: reactive[str] = reactive("")
    model: reactive[str] = reactive("")
    ctx_pct: reactive[int] = reactive(0)

    def _fmt_elapsed(self) -> str:
        s = int(self.elapsed)
        if s < 60:
            return f"{s:02d}s"
        m, s = divmod(s, 60)
        if m < 60:
            return f"{m:02d}:{s:02d}"
        h, m = divmod(m, 60)
        return f"{h}h{m:02d}"

    def _fmt_ctx(self) -> str:
        pct = self.ctx_pct
        text = f"ctx {pct}%"
        if pct >= 95:
            return f"[red]{text}[/red]"
        if pct >= 80:
            return f"[yellow]{text}[/yellow]"
        return text

    def render(self) -> str:  # type: ignore[override]
        width = self.size.width if self.size.width else 80

        parts: list[str] = [self.state]
        if self.state != "idle" and self.elapsed > 0:
            parts.append(self._fmt_elapsed())
        if self.cwd:
            parts.append(self.cwd)
        if self.hostname:
            parts.append(f"@{self.hostname}")
        if self.model:
            parts.append(self.model)
        parts.append(self._fmt_ctx())

        def plain_len(s: str) -> int:
            import re
            return len(re.sub(r"\[/?[a-z]+\]", "", s))

        def total_len(items: list[str]) -> int:
            return sum(plain_len(p) for p in items) + 3 * (len(items) - 1) + 2

        tail = parts.pop()
        while total_len(parts + [tail]) > width and len(parts) > 2:
            if len(parts) >= 4 and parts[-1] == self.model:
                parts.pop()
            elif len(parts) >= 3 and parts[-1].startswith("@"):
                parts.pop()
            elif len(parts) >= 3 and self.cwd not in ("~", ""):
                for i, p in enumerate(parts):
                    if p == self.cwd:
                        parts[i] = "~"
                        break
            else:
                break
        parts.append(tail)
        return "  " + " · ".join(parts)


# ---------------------------------------------------------------------------
# Slash command + ! prefix handlers
# ---------------------------------------------------------------------------

HELP_TEXT = """\
[help] OpsBridge slash commands

  /model              open paginated model picker (picks persist)
  /model <id>         switch to <id> AND persist to config.toml
  /model session <id> switch for this session only (no config write)
  /quit /exit /q      end the session
  /help /?            show this help

Direct exec:
  !<cmd>              run <cmd> via bash, skipping the LLM
                      (e.g. !tail -f /var/log/syslog)
                      audit chain still fires; no ask form.

Hotkeys:
  Enter               send the current input
  Shift+Enter         insert a newline
  Ctrl-D ×2           quit (first press arms; second within 2s exits)
  Ctrl-C              cancel inline form / interrupt task / clear input

Layout (§16 stream UI):
  > <user>            operator input
  ● <Tool>(args)      tool invocation
  ⎿  <result>          tool result (or bash output)
  ※ <notice>          system / transcript / help
  ● <error>           error or post-kill (red)
  ✻ Thinking…         transient — disappears on turn end

Audit log:
  /var/log/opsbridge/agent/<session-id>.jsonl

Preferences:
  /etc/opsbridge/agent/preferences.md  (mutate only via `remember`)
"""


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class OpsBridgeApp(App):
    """Claude-Code-style stream UI (PRD-phase3 §16)."""

    CSS = """
    Screen { layout: vertical; }
    VerticalScroll#stream {
        height: 1fr;
        padding: 0 1;
    }
    StatusBar#status {
        height: 1;
        background: $primary;
        color: $text;
        dock: bottom;
    }
    PromptInput#prompt {
        dock: bottom;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel", "Cancel", priority=True),
        Binding("ctrl+d", "quit", "Quit (×2)", priority=True),
    ]

    QUIT_ARM_WINDOW_SEC = 2.0
    HEARTBEAT_INTERVAL_SEC = 1.0
    MAX_QUEUE_DEPTH = 5
    IME_DUPLICATE_WINDOW_SEC = 3.0

    def __init__(
        self,
        *,
        hostname: str,
        model_label: str,
        on_operator_turn: Callable[[str], None],
        on_cancel: Callable[[], None],
        on_direct_bash: Callable[[str], None] | None = None,
        on_model_swap: Callable[[str, bool], None] | None = None,
        discover_models: Callable[[], list[str]] | None = None,
        title: str = "OpsBridge",
    ) -> None:
        super().__init__()
        self._hostname = hostname
        self._model_label = model_label
        self._on_operator_turn = on_operator_turn
        self._on_cancel = on_cancel
        self._on_direct_bash = on_direct_bash
        self._on_model_swap = on_model_swap
        self._discover_models = discover_models
        self.title = f"{title} · {hostname} · {model_label}"

        self._active_ask: AskForm | None = None
        self._active_picker: ModelPicker | None = None
        self._active_thinking: ResponseStatus | None = None
        self._think_started_at: float | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._quit_armed_at: float | None = None
        self._state_start_time: float | None = None

        # §5 queue tracker.
        self._in_flight: int = 0

        # IME / voice-input dedupe.
        self._last_submit_text: str = ""
        self._last_submit_at: float = 0.0
        self._input_value_changed_since_submit: bool = False

    # ----- composition ----------------------------------------------------

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="stream")
        yield PromptInput(id="prompt")
        yield StatusBar(id="status")

    async def on_mount(self) -> None:
        bar = self.query_one(StatusBar)
        bar.hostname = self._hostname
        bar.model = self._model_label
        self.query_one(PromptInput).focus()
        self._heartbeat_task = asyncio.create_task(self._tick_heartbeat())

    async def on_unmount(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()

    async def _tick_heartbeat(self) -> None:
        """1Hz: advance the elapsed clock on the status bar."""
        try:
            while True:
                bar = self.query_one(StatusBar)
                if bar.state != "idle" and self._state_start_time is not None:
                    bar.elapsed = time.monotonic() - self._state_start_time
                await asyncio.sleep(self.HEARTBEAT_INTERVAL_SEC)
        except asyncio.CancelledError:
            pass

    # ----- typed append surface (mount widgets into #stream) --------------

    def _mount_to_stream(self, widget) -> None:
        """Mount a widget into #stream and scroll to end. Main-thread only.

        Sync entry point; the `AwaitMount` returned by `mount()` is left
        un-awaited (the textual loop completes it on its next tick).
        Callers that need immediate visibility await `_amount_to_stream`.
        """
        try:
            stream = self.query_one("#stream", VerticalScroll)
        except Exception:  # noqa: BLE001
            return
        stream.mount(widget)
        stream.scroll_end(animate=False)

    async def _amount_to_stream(self, widget) -> None:
        """Async mount — guarantees the widget is in the DOM on return."""
        try:
            stream = self.query_one("#stream", VerticalScroll)
        except Exception:  # noqa: BLE001
            return
        await stream.mount(widget)
        stream.scroll_end(animate=False)

    def _safe_main(self, fn, *args, **kwargs) -> None:
        """Run `fn(*args, **kwargs)` on the main loop. Thread-safe."""
        try:
            self.call_from_thread(fn, *args, **kwargs)
        except RuntimeError:
            # Already on the main thread, or no loop running yet.
            try:
                fn(*args, **kwargs)
            except Exception:  # noqa: BLE001
                pass

    # Public typed surface — thread-safe entry points.

    def append_user(self, text: str) -> None:
        self._safe_main(self._mount_to_stream, UserMessage(text=text))

    def append_assistant(self, text: str) -> None:
        if not text:
            return
        self._safe_main(self._mount_to_stream, AssistantMessage(text=text))

    def append_tool_call(self, tool: str, args_summary: str = "") -> None:
        self._safe_main(
            self._mount_to_stream,
            ToolCallMessage(tool=tool, args_summary=args_summary),
        )

    def append_tool_result(self, text: str) -> None:
        self._safe_main(self._mount_to_stream, ToolResultMessage(text=text))

    def append_bash_output(self, text: str) -> None:
        self._safe_main(self._mount_to_stream, BashOutputLine(text=text))

    def append_system(self, text: str) -> None:
        self._safe_main(self._mount_to_stream, SystemNotice(text=text))

    def append_error(self, text: str) -> None:
        self._safe_main(self._mount_to_stream, ErrorMessage(text=text))

    def begin_thinking(self) -> None:
        self._think_started_at = time.monotonic()
        widget = ResponseStatus()
        self._active_thinking = widget
        self._safe_main(self._mount_to_stream, widget)

    def end_thinking(self, elapsed_s: float | None = None) -> None:
        if elapsed_s is None and self._think_started_at is not None:
            elapsed_s = time.monotonic() - self._think_started_at
        self._think_started_at = None

        def _remove():
            if self._active_thinking is not None:
                try:
                    self._active_thinking.remove()
                except Exception:  # noqa: BLE001
                    pass
                self._active_thinking = None
            secs = f"{elapsed_s:.1f}s" if elapsed_s is not None else "—"
            self._mount_to_stream(SystemNotice(text=f"done · {secs}"))

        self._safe_main(_remove)

    # ----- backward-compat shim ------------------------------------------

    def write_top(self, line: str, *, kind: str = "bash_out") -> None:
        """Backwards-compat dispatch to the typed surface.

        Kept so legacy callers in core.py / tools.py / older tests still
        route correctly during the §16 migration. New callers should use
        the typed `append_*` methods directly.
        """
        dispatch = {
            "user": self.append_user,
            "ai": self.append_assistant,
            "bash_cmd": lambda t: self.append_tool_call("Bash", t.lstrip("$ ").strip()),
            "bash_out": self.append_bash_output,
            "tool": self.append_system,
            "system": self.append_system,
        }
        fn = dispatch.get(kind, self.append_bash_output)
        fn(line)

    # Compatibility for `set_final_answer` callers in core.py.
    def set_final_answer(self, text: str) -> None:
        self.append_assistant(text)

    # ----- status bar wires ----------------------------------------------

    def set_status(self, state: str, detail: str = "") -> None:
        self._safe_main(self._do_set_status, state)

    def _do_set_status(self, state: str) -> None:
        try:
            bar = self.query_one(StatusBar)
            if bar.state != state:
                bar.state = state
                if state == "idle":
                    self._state_start_time = None
                    bar.elapsed = 0.0
                else:
                    self._state_start_time = time.monotonic()
                    bar.elapsed = 0.0
        except Exception:  # noqa: BLE001
            pass

    def set_cwd(self, cwd: str) -> None:
        self._safe_main(self._do_set_cwd, cwd)

    def _do_set_cwd(self, cwd: str) -> None:
        try:
            self.query_one(StatusBar).cwd = _abbreviate_cwd(cwd)
        except Exception:  # noqa: BLE001
            pass

    def set_model(self, model: str) -> None:
        self._safe_main(self._do_set_model, model)

    def _do_set_model(self, model: str) -> None:
        try:
            self.query_one(StatusBar).model = model
        except Exception:  # noqa: BLE001
            pass

    def set_context_percent(self, pct: int) -> None:
        self._safe_main(self._do_set_ctx, pct)

    def _do_set_ctx(self, pct: int) -> None:
        try:
            self.query_one(StatusBar).ctx_pct = max(0, min(100, int(pct)))
        except Exception:  # noqa: BLE001
            pass

    def notify_turn_done(self) -> None:
        self._safe_main(self._do_decrement_queue)

    def _do_decrement_queue(self) -> None:
        if self._in_flight > 0:
            self._in_flight -= 1

    # ----- ask form (inline) ---------------------------------------------

    def show_ask_form(self, prompt: str, options: list[str]) -> str:
        """Block the agent thread until the operator answers."""
        event = threading.Event()
        slot: dict = {"choice": None}

        def _on_resolve(choice: str) -> None:
            slot["choice"] = choice
            self._active_ask = None
            self._do_set_status("idle")
            event.set()

        def _activate():
            widget = AskForm(prompt=prompt, options=list(options), on_resolve=_on_resolve)
            self._active_ask = widget
            self._do_set_status("awaiting input")
            self._mount_to_stream(widget)
            try:
                widget.focus()
            except Exception:  # noqa: BLE001
                pass

        self._safe_main(_activate)
        event.wait()
        return slot["choice"] or options[0]

    def _resolve_ask(self, choice: str) -> None:
        """Imperative resolve — used by Ctrl-C cascade for cancel."""
        if self._active_ask is None:
            return
        self._active_ask.resolve(choice)

    # ----- model picker (inline) -----------------------------------------

    def _open_model_picker(self) -> None:
        models: list[str] = []
        if self._discover_models is not None:
            try:
                models = self._discover_models() or []
            except Exception:  # noqa: BLE001
                models = []
        if not models:
            self.append_system(
                "[/model] couldn't discover models — type `/model <id>` to set manually"
            )
            return

        current = ""
        try:
            current = self.query_one(StatusBar).model
        except Exception:  # noqa: BLE001
            current = self._model_label

        def _on_pick(mid: str) -> None:
            self._active_picker = None
            self._do_set_status("idle")
            if self._on_model_swap is not None:
                self._on_model_swap(mid, True)
                self._do_set_model(mid)
            self.append_system(f"[/model] switched to {mid} (persisted)")
            try:
                self.query_one(PromptInput).focus()
            except Exception:  # noqa: BLE001
                pass

        def _on_cancel() -> None:
            if self._active_picker is not None:
                try:
                    self._active_picker.remove()
                except Exception:  # noqa: BLE001
                    pass
                self._active_picker = None
            self._do_set_status("idle")
            self.append_system("[/model] cancelled")
            try:
                self.query_one(PromptInput).focus()
            except Exception:  # noqa: BLE001
                pass

        widget = ModelPicker(
            models=models,
            current_model=current,
            on_pick=_on_pick,
            on_cancel=_on_cancel,
        )
        self._active_picker = widget
        self._do_set_status("awaiting input")
        self._mount_to_stream(widget)
        try:
            widget.focus()
        except Exception:  # noqa: BLE001
            pass

    # ----- /model command handling ---------------------------------------

    def _handle_model_command(self, args: str) -> None:
        if self._on_model_swap is None:
            self.append_system("[/model] not available (no agent wired)")
            return
        if not args:
            self._open_model_picker()
            return
        parts = args.split(maxsplit=1)
        if parts[0] == "session" and len(parts) == 2:
            new_id = parts[1].strip()
            if new_id:
                self.append_system(f"[/model] switching to {new_id} (session only)")
                self._on_model_swap(new_id, False)
                self._do_set_model(new_id)
            return
        if parts[0] == "save" and len(parts) == 2:
            new_id = parts[1].strip()
            if new_id:
                self.append_system(f"[/model] switching to {new_id} (persist)")
                self._on_model_swap(new_id, True)
                self._do_set_model(new_id)
            return
        new_id = args.strip()
        if new_id:
            self.append_system(f"[/model] switching to {new_id} (persist)")
            self._on_model_swap(new_id, True)
            self._do_set_model(new_id)

    # ----- prompt input handling -----------------------------------------

    async def on_prompt_input_submitted(self, message: PromptInput.Submitted) -> None:
        await self._handle_submission(message.text)

    async def _handle_submission(self, text: str) -> None:
        text = text.rstrip()
        if not text.strip():
            return

        # IME / voice-input dedupe.
        now = time.monotonic()
        is_duplicate = (
            text == self._last_submit_text
            and not self._input_value_changed_since_submit
            and (now - self._last_submit_at) < self.IME_DUPLICATE_WINDOW_SEC
        )
        if is_duplicate:
            self.append_system("[ime duplicate suppressed]")
            return
        self._last_submit_text = text
        self._last_submit_at = now
        self._input_value_changed_since_submit = False

        # Any submit disarms the Ctrl-D quit window.
        self._quit_armed_at = None

        stripped = text.strip()
        if stripped in ("/quit", "/exit", "/q"):
            self.exit()
            return
        if stripped in ("/help", "/?"):
            await self._amount_to_stream(UserMessage(text=stripped))
            for line in HELP_TEXT.splitlines():
                await self._amount_to_stream(SystemNotice(text=line))
            return
        if stripped == "/model" or stripped.startswith("/model "):
            await self._amount_to_stream(UserMessage(text=stripped))
            self._handle_model_command(stripped[len("/model"):].strip())
            return

        # Queue bound.
        if self._in_flight >= self.MAX_QUEUE_DEPTH:
            await self._amount_to_stream(SystemNotice(
                text=(
                    f"[queue full — {self._in_flight} pending. "
                    "Press Ctrl-C to cancel the current task before queuing more.]"
                )
            ))
            return

        # `!` prefix → direct bash. `\!` escapes.
        if stripped.startswith("\\!"):
            text = stripped[1:]
        elif stripped.startswith("!"):
            cmd = stripped[1:].strip()
            if not cmd:
                return
            self._in_flight += 1
            queue_hint = (
                f"  (queued — {self._in_flight - 1} ahead)" if self._in_flight > 1 else ""
            )
            await self._amount_to_stream(UserMessage(text=f"! {cmd}{queue_hint}"))
            if self._on_direct_bash is not None:
                self._on_direct_bash(cmd)
            return

        self._in_flight += 1
        queue_hint = (
            f"  (queued — {self._in_flight - 1} ahead)" if self._in_flight > 1 else ""
        )
        await self._amount_to_stream(UserMessage(text=f"{text}{queue_hint}"))
        self._do_set_status("thinking")
        self._on_operator_turn(text)

    # Backward-compat for batch_e IME tests that call on_input_submitted.
    async def on_input_submitted(self, message) -> None:
        """Legacy entry — accepts a Submitted-like message with `.value`
        or `.text`. Routes through the §16 submission path.
        """
        text = getattr(message, "value", None)
        if text is None:
            text = getattr(message, "text", "")
        await self._handle_submission(text or "")

    async def on_input_changed(self, event) -> None:
        """Legacy: tracks whether new content appeared since last submit."""
        value = getattr(event, "value", None)
        if value is None:
            value = getattr(event, "text", "")
        if value:
            self._input_value_changed_since_submit = True

    # ----- key handling ---------------------------------------------------

    async def action_cancel(self) -> None:
        """Ctrl-C cascade (PRD-phase3 §16, ported from §15).

        Order: modal-cancel → interrupt-running → clear-input → hint.
        Never exits — Ctrl-D ×2 is the only exit.
        """
        if self._active_ask is not None:
            self._active_ask.resolve("__cancelled__" if "__cancelled__" in self._active_ask.options else self._active_ask.options[0])
            return
        if self._active_picker is not None:
            self._active_picker._cancel()
            return
        if self._in_flight > 0:
            self._on_cancel()
            self._do_set_status("idle")
            return
        try:
            inp = self.query_one(PromptInput)
        except Exception:  # noqa: BLE001
            return
        if inp.text:
            inp.clear()
            return
        await self._amount_to_stream(SystemNotice(text="[Ctrl-D to quit · /help for commands]"))

    async def action_quit(self) -> None:
        """Ctrl-D handler with two-press confirmation."""
        now = time.monotonic()
        if self._quit_armed_at is not None and (now - self._quit_armed_at) <= self.QUIT_ARM_WINDOW_SEC:
            self.exit()
            return
        self._quit_armed_at = now
        self.append_system(
            f"[press Ctrl-D again within {int(self.QUIT_ARM_WINDOW_SEC)}s to quit · or type /quit]"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _abbreviate_cwd(cwd: str, max_len: int = 28) -> str:
    """Shorten a path for the status row. HOME → ~; long paths → middle-ellipsis."""
    if not cwd:
        return ""
    import os
    home = os.path.expanduser("~")
    if cwd == home:
        return "~"
    if cwd.startswith(home + "/"):
        cwd = "~" + cwd[len(home):]
    if len(cwd) <= max_len:
        return cwd
    parts = cwd.split("/")
    if len(parts) <= 3:
        return cwd[: max_len - 1] + "…"
    head, *_middle, tail2, tail1 = parts
    abbreviated = f"{head}/…/{tail2}/{tail1}"
    if len(abbreviated) <= max_len + 2:
        return abbreviated
    return cwd[: max_len - 1] + "…"
