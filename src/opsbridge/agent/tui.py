"""Textual TUI for OpsBridge agent — three regions, no chrome borders.

Phase 3 §15 layout (replaces the Phase 2 four-region bordered design):

    ─────────────────────────────────────────────────────────  ← bg=$surface
    top scrolling region (flex)
    ...
    ─────────────────────────────────────────────────────────  ← bg=$boost
    middle (final answer / ask form, 0 rows if empty)
    ─────────────────────────────────────────────────────────  ← bg=$primary
    ◐ <state> · <elapsed> · <cwd> · @<host> · <model> · ctx <%>
    ─────────────────────────────────────────────────────────  ← bg=$surface
    > input_

No borders — regions distinguished by background color. Net 5 rows
back to scroll history on a 24-row terminal vs. Phase 2.

Threading model (unchanged from Phase 2): agent runs on a background
daemon thread; rendering and operator input live on textual's asyncio
loop. Cross-thread communication via `App.call_from_thread`.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Callable

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import Input, RichLog, Static

from rich.markup import escape as _rich_escape


# ---------------------------------------------------------------------------
# Top-region line styling (Subtle palette)
# ---------------------------------------------------------------------------
# Visual differentiation between operator input, AI agent response, bash
# echo / output, and tool chatter (search / visit / system notices).
# Subtle palette: tinted dim backgrounds for "human dialogue" (user + AI),
# foreground accent only for bash command echo, dim foreground for tool
# chatter and system notices. Avoids cognitive load on long sessions.

_TOP_LOG_STYLES: dict[str, str] = {
    # Backgrounds use explicit hex so the difference shows reliably across
    # textual's dark themes (the $boost / $surface tokens are too close to
    # $surface on many of the built-in themes to be visually distinct).
    "user":     "on #1a2a40",             # user input — desaturated blue tint
    "ai":       "on #1a3a25",             # AI response — desaturated green tint
    "bash_cmd": "bold cyan",              # `$ ...` echo — accent fg, no bg
    "bash_out": "",                        # raw bash output — neutral
    "tool":     "dim",                     # [search]/[visit] chatter — muted
    "system":   "yellow",                  # [help]/[queue full]/[ctx …] — warm
}

try:
    from wcwidth import wcswidth as _wcswidth
except ImportError:  # pragma: no cover — declared dependency
    def _wcswidth(s: str) -> int:
        return len(s)


class WidthAwareInput(Input):
    """Phase 3 §10: Input that forces a full layout refresh on edits.

    textual's default Input widget computes cursor / erase column math
    assuming each codepoint is column-width 1. That holds for ASCII but
    not for CJK (wide=2), emoji (wide=2 mostly), or combining sequences.
    Result: after Backspace on a Chinese character, the right half of
    the wide glyph stays painted on screen as half-glyph garbage.

    Forcing `refresh(layout=True)` after every edit-action makes the
    widget recompute its render rectangle, which clears the stale cells.
    Slightly more expensive than the default but cheap enough for an
    input line.

    The CJK-typing operator's quality of life is worth more than the few
    extra repaints per keystroke.
    """

    def action_delete_left(self) -> None:
        super().action_delete_left()
        self.refresh(layout=True)

    def action_delete_left_word(self) -> None:
        super().action_delete_left_word()
        self.refresh(layout=True)

    def action_delete_left_all(self) -> None:
        super().action_delete_left_all()
        self.refresh(layout=True)

    def action_delete_right(self) -> None:
        super().action_delete_right()
        self.refresh(layout=True)

    async def _on_paste(self, event) -> None:  # type: ignore[override]
        """Force a layout refresh after a paste lands, so CJK column math
        comes out right (IMEs and voice-input on macOS/Linux all emit
        Paste events).

        textual 8.x's `Input._on_paste` is NOT a coroutine — calling it
        returns None, so `await super()._on_paste(...)` raises TypeError.
        Newer textual versions may make it async; we inspect-then-await
        defensively so the override survives both shapes.
        """
        import inspect
        try:
            result = super()._on_paste(event)
            if inspect.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001 — never let our refresh wrapper take down the input
            pass
        self.refresh(layout=True)


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
# Status row — consolidated header + status bar (Phase 3 §15)
# ---------------------------------------------------------------------------

class StatusBar(Static):
    """One-row consolidated status: spinner · state · elapsed · cwd · @host · model · ctx%.

    Fields drop right-to-left under width pressure (model first → @host →
    cwd collapses to ~). ctx% switches foreground colour at the budget
    thresholds (yellow ≥80%, red ≥90%).
    """

    state: reactive[str] = reactive("idle")
    elapsed: reactive[float] = reactive(0.0)   # seconds since state became non-idle
    cwd: reactive[str] = reactive("~")
    hostname: reactive[str] = reactive("")
    model: reactive[str] = reactive("")
    ctx_pct: reactive[int] = reactive(0)
    spinner_frame: reactive[int] = reactive(0)

    SPINNER = "◐◓◑◒"

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
        glyph = self.SPINNER[self.spinner_frame % len(self.SPINNER)] if self.state != "idle" else " "
        width = self.size.width if self.size.width else 80

        # Build candidate fields in priority order. Truncation = drop trailing
        # fields until total fits.
        parts: list[str] = [f"{glyph} {self.state}"]
        if self.state != "idle" and self.elapsed > 0:
            parts.append(self._fmt_elapsed())
        if self.cwd:
            parts.append(self.cwd)
        if self.hostname:
            parts.append(f"@{self.hostname}")
        if self.model:
            parts.append(self.model)
        parts.append(self._fmt_ctx())

        # Visible-length-aware truncation. We compute on the plain-text
        # version (sans Rich markup) so [yellow]/[red] tags don't count.
        def plain_len(s: str) -> int:
            import re
            return len(re.sub(r"\[/?[a-z]+\]", "", s))

        def total_len(items: list[str]) -> int:
            return sum(plain_len(p) for p in items) + 3 * (len(items) - 1) + 2

        # Drop fields right-to-left, but keep ctx% (always tail).
        # Order of sacrifice: model → @host → collapse cwd to ~.
        tail = parts.pop()  # ctx
        while total_len(parts + [tail]) > width and len(parts) > 2:
            # Drop the rightmost non-essential field. Essentials are spinner+state
            # and ctx (the tail). Collapse cwd to "~" before dropping further.
            if len(parts) >= 4 and parts[-1] == self.model:
                parts.pop()
            elif len(parts) >= 3 and parts[-1].startswith("@"):
                parts.pop()
            elif len(parts) >= 3 and self.cwd not in ("~", ""):
                # Collapse cwd to ~ (or drop entirely if it was already ~).
                # cwd lives at index 2 if elapsed is present, 1 otherwise.
                for i, p in enumerate(parts):
                    if p == self.cwd:
                        parts[i] = "~"
                        break
            else:
                # Out of fat to trim; let it overflow.
                break
        parts.append(tail)
        return "  " + " · ".join(parts)


class TopLog(RichLog):
    """Scrollable top region — bash output, search/visit chatter, etc."""


class MiddlePanel(Static):
    """Middle region — final answer or ask form. Empty → 0 height."""

    def show_text(self, text: str) -> None:
        self.update(text or "")
        # Hide entirely when empty (§15 — no wasted rows on idle middle).
        self.display = bool(text)

    def show_form(self, state: _AskState) -> None:
        self.update(_render_form(state))
        self.display = True

    def clear(self) -> None:
        self.update("")
        self.display = False


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
# Slash-command + !-prefix handlers
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
  Ctrl-D ×2           quit (first press arms; second within 2s exits)
  Ctrl-C              cancel current ask form / running bash

Layout:
  status row          ◐ state · elapsed · cwd · @host · model · ctx%
                      fields drop right→left under width pressure.
                      ctx turns yellow ≥80%, red ≥95%.

Audit log:
  /var/log/opsbridge/agent/<session-id>.jsonl

Preferences:
  /etc/opsbridge/agent/preferences.md  (mutate only via `remember`)
"""


# ---------------------------------------------------------------------------
# Model picker — phase-3 §11
# ---------------------------------------------------------------------------

@dataclass
class _PickerState:
    """In-flight model-picker state. Lives in the middle region while open."""
    models: list[str]
    selected_idx: int = 0          # index into models[]
    page: int = 0                  # current page (0-based)
    page_size: int = 10
    current_model: str = ""         # to highlight the active one

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
        # Keep page in sync with selection.
        self.page = self.selected_idx // self.page_size

    def page_relative(self, delta: int) -> None:
        new_page = max(0, min(self.total_pages - 1, self.page + delta))
        if new_page != self.page:
            self.page = new_page
            self.selected_idx = self.page * self.page_size


def _render_picker(state: _PickerState) -> str:
    """Render the picker form for display in the middle region."""
    lines = ["▶ Switch model (Enter applies, Esc cancels):", ""]
    if not state.models:
        lines.append("  (no models discovered — type `/model <id>` to set manually)")
        return "\n".join(lines)
    start, _end = state.page_slice()
    for i, mid in state.visible():
        marker = "•" if i == state.selected_idx else " "
        suffix = " ← current" if mid == state.current_model else ""
        number = i - start + 1   # 1-indexed within the page
        lines.append(f"  ({marker}) [{number}] {mid}{suffix}")
    lines.append("")
    if state.total_pages > 1:
        lines.append(
            f"page {state.page + 1}/{state.total_pages}  (n=next, p=prev, "
            f"1-{min(state.page_size, len(state.models) - start)}=pick, Esc=cancel)"
        )
    else:
        lines.append("(1-9 to pick directly, Enter to apply selection, Esc to cancel)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class OpsBridgeApp(App):
    """Three-region TUI: top / middle / status+input."""

    CSS = """
    Screen { layout: vertical; }
    /* No borders anywhere — regions distinguished by background. Explicit
       hex backgrounds because textual's $surface / $boost are too close
       on many built-in themes to be visually distinct. */
    #top_log {
        height: 1fr;
        background: #0e0e10;            /* base */
        padding: 0 1;
    }
    #middle {
        height: auto;
        max-height: 30%;
        background: #18222e;            /* clearly distinct from top */
        padding: 0 1;
        color: $text;
    }
    StatusBar {
        height: 1;
        background: $primary;
        color: $text;
    }
    Input {
        dock: bottom;
        background: #1a1a1c;            /* slightly lighter than top */
        border: none;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel", "Cancel", priority=True),
        # priority=True so Input doesn't swallow Ctrl-D as delete-forward-char.
        Binding("ctrl+d", "quit", "Quit (×2)", priority=True),
    ]

    # Double-press window for Ctrl-D quit (seconds).
    QUIT_ARM_WINDOW_SEC = 2.0

    # Heartbeat tick cadence — drives the elapsed clock visible refresh.
    HEARTBEAT_INTERVAL_SEC = 1.0

    # Phase 3 §5: max number of operator turns pending in the agent
    # thread's queue. Past this, new submissions are rejected with a
    # polite message — prevents runaway buildup when the agent is hung.
    MAX_QUEUE_DEPTH = 5

    # Wider safety-net window for the time-based dedupe. The primary
    # dedupe is now "no keystrokes between two identical submits" (see
    # _last_submit_* in __init__ + on_input_submitted), which catches
    # IME / voice-input duplicates regardless of delay. The time window
    # below is a backup for environments that swallow our Key events.
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
        # §11 /model: callback (new_id, persist_to_config) — None disables the
        # feature (e.g., in unit tests that don't wire an agent).
        self._on_model_swap = on_model_swap
        # §11: optional callback that returns the discoverable model list.
        # Called from the App's input handler; should be fast (cached/synchronous).
        self._discover_models = discover_models
        # Window title (visible in tmux/terminal title bar). The on-screen
        # equivalent (hostname/model) lives in the consolidated status row.
        self.title = f"{title} · {hostname} · {model_label}"
        self._active_ask: _AskState | None = None
        self._active_picker: _PickerState | None = None
        self._spinner_task: asyncio.Task | None = None
        self._quit_armed_at: float | None = None
        self._state_start_time: float | None = None
        # §5 queue-depth tracker. Bumped at submit, decremented when the
        # agent thread reports turn_end via `notify_turn_done`.
        self._in_flight: int = 0
        # IME / voice-input dedupe state. macOS Terminal + Chinese IMEs and
        # voice-input modes queue `Input.Submitted` TWICE for one Enter
        # (both messages capture the value at queue time, before our
        # value-clear runs). We dedupe a Submitted as duplicate iff:
        #
        #   - text matches the previous submit, AND
        #   - the input value hasn't received NEW content since the last
        #     submit (`_input_value_changed_since_submit` False).
        #
        # The flag is flipped True by `on_input_changed` whenever the
        # widget's value becomes non-empty (i.e., real user/IME composition).
        # Our own `message.input.value = ""` clear is non-empty → empty,
        # which is filtered out so it doesn't break the dedupe.
        #
        # Time window kept as a belt-and-suspenders bound for environments
        # where Input.Changed somehow doesn't fire (theoretical safety).
        self._last_submit_text: str = ""
        self._last_submit_at: float = 0.0
        self._input_value_changed_since_submit: bool = False

    # ----- composition ----------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical():
            # markup=True so we can colorise per-line by `kind` (user / ai /
            # bash_cmd / bash_out / tool / system) — see _TOP_LOG_STYLES.
            # Untrusted bash output is escape()'d before insertion so
            # square brackets in apt/dpkg output don't get parsed as tags.
            yield TopLog(id="top_log", wrap=True, highlight=False, markup=True)
            yield MiddlePanel(id="middle")
            yield StatusBar(id="status")
        # WidthAwareInput (§10) — handles CJK/emoji backspace residue.
        yield WidthAwareInput(id="prompt", placeholder="ask the agent — /help for commands")

    async def on_mount(self) -> None:
        bar = self.query_one(StatusBar)
        bar.hostname = self._hostname
        bar.model = self._model_label
        # Empty middle starts collapsed.
        mid = self.query_one(MiddlePanel)
        mid.display = False
        self.query_one(Input).focus()
        self._spinner_task = asyncio.create_task(self._tick_spinner())

    async def on_unmount(self) -> None:
        if self._spinner_task is not None:
            self._spinner_task.cancel()

    async def _tick_spinner(self) -> None:
        """1 Hz tick: advance spinner glyph + update elapsed clock."""
        try:
            while True:
                bar = self.query_one(StatusBar)
                if bar.state != "idle":
                    bar.spinner_frame = bar.spinner_frame + 1
                    if self._state_start_time is not None:
                        bar.elapsed = time.monotonic() - self._state_start_time
                await asyncio.sleep(self.HEARTBEAT_INTERVAL_SEC)
        except asyncio.CancelledError:
            pass

    # ----- public methods (called from agent thread via call_from_thread) -

    def write_top(self, line: str, *, kind: str = "bash_out") -> None:
        """Append a line to the top region, styled by `kind`.

        Kinds (see _TOP_LOG_STYLES for the palette):
          - user      operator-typed input (`> ...`)
          - ai        AI agent narration / mid-turn response
          - bash_cmd  `$ <command>` echo before exec
          - bash_out  raw bash stdout/stderr (default — no styling)
          - tool      [search] / [visit] / similar tool chatter
          - system    [help] / [queue full] / [context …] notices

        Safe from any thread. If called from the App's own loop thread
        (e.g., from on_input_submitted), call_from_thread raises and we
        fall through to a direct synchronous call.
        """
        style = _TOP_LOG_STYLES.get(kind, "")
        escaped = _rich_escape(line)
        formatted = f"[{style}]{escaped}[/{style}]" if style else escaped
        try:
            self.call_from_thread(self._do_write_top, formatted)
            return
        except RuntimeError:
            pass
        # Same-thread fallback (App handler invoking write_top directly).
        # Distinguish "app running, just same-thread" from "app not started":
        # if we can find the TopLog widget, write to it; else fall through
        # to stdout (for unit tests / pre-mount calls).
        try:
            log = self.query_one(TopLog)
        except Exception:  # noqa: BLE001 — no app / widget not in tree yet
            print(line)
            return
        try:
            log.write(formatted)
        except Exception:  # noqa: BLE001
            print(line)

    def _do_write_top(self, line: str) -> None:
        try:
            self.query_one(TopLog).write(line)
        except Exception:  # noqa: BLE001
            pass

    def set_status(self, state: str, detail: str = "") -> None:
        """Set status + optional detail. Resets the elapsed clock on transitions.

        `detail` is intentionally unused in the new layout — kept in the
        signature for backward compatibility with Phase 2 callers. The
        Phase 3 status row composes its own fields from reactive attrs.
        """
        try:
            self.call_from_thread(self._do_set_status, state)
        except RuntimeError:
            pass

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
        """Update the cwd field in the status row. Pre-formatted (HOME → ~)."""
        try:
            self.call_from_thread(self._do_set_cwd, cwd)
        except RuntimeError:
            pass

    def _do_set_cwd(self, cwd: str) -> None:
        try:
            self.query_one(StatusBar).cwd = _abbreviate_cwd(cwd)
        except Exception:  # noqa: BLE001
            pass

    def set_model(self, model: str) -> None:
        """Update the model id in the status row (for /model swaps)."""
        try:
            self.call_from_thread(self._do_set_model, model)
        except RuntimeError:
            pass

    def _do_set_model(self, model: str) -> None:
        try:
            self.query_one(StatusBar).model = model
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
            if text:
                mid.show_text(text)
            else:
                mid.clear()
        except Exception:  # noqa: BLE001
            pass

    def set_context_percent(self, pct: int) -> None:
        try:
            self.call_from_thread(self._do_set_ctx, pct)
        except RuntimeError:
            pass

    def _do_set_ctx(self, pct: int) -> None:
        try:
            self.query_one(StatusBar).ctx_pct = max(0, min(100, int(pct)))
        except Exception:  # noqa: BLE001
            pass

    def notify_turn_done(self) -> None:
        """Called by the agent thread when a turn (LLM or `!`-direct) ends.

        Decrements the in-flight counter so §5's queue indicator releases.
        """
        try:
            self.call_from_thread(self._do_decrement_queue)
        except RuntimeError:
            pass

    def _do_decrement_queue(self) -> None:
        if self._in_flight > 0:
            self._in_flight -= 1

    # ----- ask form -------------------------------------------------------

    def show_ask_form(self, prompt: str, options: list[str]) -> str:
        """Block the agent thread until the operator answers."""
        event = threading.Event()
        slot: dict = {"choice": None}
        state = _AskState(prompt=prompt, options=list(options), event=event, chosen=slot)
        try:
            self.call_from_thread(self._activate_ask_form, state)
        except RuntimeError:
            return options[0]
        event.wait()
        return slot["choice"] or options[0]

    def _activate_ask_form(self, state: _AskState) -> None:
        self._active_ask = state
        self._do_set_status("awaiting input")
        try:
            self.query_one(MiddlePanel).show_form(state)
        except Exception:  # noqa: BLE001
            pass

    def _resolve_ask(self, choice: str) -> None:
        if self._active_ask is None:
            return
        state = self._active_ask
        self._active_ask = None
        state.chosen["choice"] = choice
        state.event.set()
        self._do_set_status("idle")
        try:
            self.query_one(MiddlePanel).clear()
        except Exception:  # noqa: BLE001
            pass

    # ----- key handling for the ask form ----------------------------------

    async def on_input_changed(self, event: Input.Changed) -> None:
        """Track whether the input value gained NEW content since the
        last accepted submit. Used by the IME-duplicate dedupe in
        on_input_submitted. We ignore the empty-string event our own
        `value = ""` clear emits — that isn't "new operator content".
        """
        if event.value:
            self._input_value_changed_since_submit = True

    async def on_key(self, event) -> None:
        # Picker key handling takes precedence when active.
        if self._active_picker is not None:
            await self._picker_key(event)
            return
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
        if key.lower() == "y" and any(o.lower().startswith("y") for o in state.options):
            for i, o in enumerate(state.options):
                if o.lower().startswith("y"):
                    state.selected_idx = i
                    break
            self._refresh_form()
            event.stop()
            return
        if key.lower() == "n" and any(o.lower().startswith("n") for o in state.options):
            for i, o in enumerate(state.options):
                if o.lower().startswith("n"):
                    state.selected_idx = i
                    break
            self._refresh_form()
            event.stop()
            return
        if key == "enter":
            self._resolve_ask(state.options[state.selected_idx])
            event.stop()
            return

    def _refresh_form(self) -> None:
        if self._active_ask is None:
            return
        try:
            self.query_one(MiddlePanel).show_form(self._active_ask)
        except Exception:  # noqa: BLE001
            pass

    # ----- picker key handling -------------------------------------------

    async def _picker_key(self, event) -> None:
        state = self._active_picker
        if state is None:
            return
        key = event.key
        if key == "escape":
            self._close_picker(None)
            self.write_top("[/model] cancelled", kind="system")
            event.stop()
            return
        if key == "enter":
            applied = state.models[state.selected_idx]
            self._close_picker(applied)
            event.stop()
            return
        if key in ("up",):
            state.select_relative(-1)
            self._refresh_picker()
            event.stop()
            return
        if key in ("down",):
            state.select_relative(1)
            self._refresh_picker()
            event.stop()
            return
        if key.lower() in ("n",):
            state.page_relative(1)
            self._refresh_picker()
            event.stop()
            return
        if key.lower() in ("p",):
            state.page_relative(-1)
            self._refresh_picker()
            event.stop()
            return
        # Numeric 1-9 picks within the current page directly.
        if key.isdigit() and key != "0":
            idx_in_page = int(key) - 1
            start, end = state.page_slice()
            target = start + idx_in_page
            if target < end:
                applied = state.models[target]
                self._close_picker(applied)
            event.stop()
            return

    # ----- input ----------------------------------------------------------

    async def on_input_submitted(self, message: Input.Submitted) -> None:
        text = message.value.rstrip()
        message.input.value = ""
        if not text.strip():
            return

        # IME / voice-input dedupe — see _last_submit_* in __init__.
        # The second Submitted from a CJK IME / voice-input arrives with
        # the same captured value but WITHOUT a new Input.Changed event
        # bumping our `_input_value_changed_since_submit` flag, because the
        # operator never typed anything new — the duplicate came from a
        # queued Submitted message, not fresh composition.
        now = time.monotonic()
        is_duplicate = (
            text == self._last_submit_text
            and not self._input_value_changed_since_submit
            and (now - self._last_submit_at) < self.IME_DUPLICATE_WINDOW_SEC
        )
        if is_duplicate:
            self.write_top("[ime duplicate suppressed]", kind="system")
            return
        self._last_submit_text = text
        self._last_submit_at = now
        self._input_value_changed_since_submit = False

        # Any keystroke past the arm window disarms quit.
        self._quit_armed_at = None

        # Slash commands handled in-process — no LLM round-trip.
        stripped = text.strip()
        if stripped in ("/quit", "/exit", "/q"):
            self.exit()
            return
        if stripped in ("/help", "/?"):
            self.write_top(f"> {stripped}", kind="user")
            for line in HELP_TEXT.splitlines():
                self.write_top(line, kind="system")
            return

        # /model — §11. Three forms:
        #   /model               → open picker (paginated)
        #   /model <id>          → swap directly (session-only)
        #   /model save <id>     → swap AND persist to config.toml
        if stripped == "/model" or stripped.startswith("/model "):
            self.write_top(f"> {stripped}", kind="user")
            self._handle_model_command(stripped[len("/model"):].strip())
            return

        # §5: bound the queue. Past MAX_QUEUE_DEPTH, refuse new turns
        # with a polite hint pointing to Ctrl-C as the escape.
        if self._in_flight >= self.MAX_QUEUE_DEPTH:
            self.write_top(
                f"[queue full — {self._in_flight} pending. "
                "Press Ctrl-C to cancel the current task before queuing more.]",
                kind="system",
            )
            return

        # `!` prefix → direct bash, skip the LLM (phase-3 §12).
        # `\!` escapes the sigil: treated as plain English.
        if stripped.startswith("\\!"):
            text = stripped[1:]   # strip the backslash; route as English
        elif stripped.startswith("!"):
            cmd = stripped[1:].strip()
            if not cmd:
                return
            self._in_flight += 1
            queue_hint = (
                f"  (queued — {self._in_flight - 1} ahead)" if self._in_flight > 1 else ""
            )
            self.write_top(f"! {cmd}{queue_hint}", kind="user")
            if self._on_direct_bash is not None:
                self._on_direct_bash(cmd)
            return

        # Echo + hand to the agent thread.
        self._in_flight += 1
        queue_hint = (
            f"  (queued — {self._in_flight - 1} ahead)" if self._in_flight > 1 else ""
        )
        self.write_top(f"> {text}{queue_hint}", kind="user")
        self._do_set_final_answer("")
        self._do_set_status("thinking")
        self._on_operator_turn(text)

    # ----- /model handling (phase-3 §11) ----------------------------------

    def _handle_model_command(self, args: str) -> None:
        """Dispatch /model parsing.

        Forms (post operator-feedback rebalance: persist is the new default):
          /model                     → open picker (picks persist)
          /model <id>                → swap AND persist to config.toml
          /model session <id>        → swap for this session only
          /model save <id>           → alias for `/model <id>` (back-compat)
        """
        if self._on_model_swap is None:
            self.write_top("[/model] not available (no agent wired)", kind="system")
            return

        if not args:
            self._open_model_picker()
            return

        parts = args.split(maxsplit=1)

        # `session <id>` → session-only swap (explicit opt-out of persist).
        if parts[0] == "session" and len(parts) == 2:
            new_id = parts[1].strip()
            if new_id:
                self.write_top(f"[/model] switching to {new_id} (session only)", kind="system")
                self._on_model_swap(new_id, False)
                self._do_set_model(new_id)
            return

        # `save <id>` → kept as an alias for explicit persist (back-compat).
        if parts[0] == "save" and len(parts) == 2:
            new_id = parts[1].strip()
            if new_id:
                self.write_top(f"[/model] switching to {new_id} (persist)", kind="system")
                self._on_model_swap(new_id, True)
                self._do_set_model(new_id)
            return

        # Plain `/model <id>` — persist by default.
        new_id = args.strip()
        if new_id:
            self.write_top(f"[/model] switching to {new_id} (persist)", kind="system")
            self._on_model_swap(new_id, True)
            self._do_set_model(new_id)

    def _open_model_picker(self) -> None:
        """Render the picker into the middle region."""
        models: list[str] = []
        if self._discover_models is not None:
            try:
                models = self._discover_models() or []
            except Exception:  # noqa: BLE001
                models = []
        if not models:
            self.write_top("[/model] couldn't discover models — type `/model <id>` to set manually", kind="system")
            return
        # Highlight the currently-active model.
        try:
            current = self.query_one(StatusBar).model
        except Exception:  # noqa: BLE001
            current = self._model_label
        state = _PickerState(models=models, current_model=current)
        # Move selection to the current model if it's in the list.
        for i, m in enumerate(state.models):
            if m == current:
                state.selected_idx = i
                state.page = i // state.page_size
                break
        self._active_picker = state
        self._do_set_status("awaiting input")
        try:
            self.query_one(MiddlePanel).update(_render_picker(state))
            self.query_one(MiddlePanel).display = True
        except Exception:  # noqa: BLE001
            pass
        # Defocus Input so character keys (digits, n, p, Esc) bubble up to
        # the App-level on_key handler instead of being typed into the
        # input field.
        try:
            self.set_focus(None)
        except Exception:  # noqa: BLE001
            pass

    def _refresh_picker(self) -> None:
        if self._active_picker is None:
            return
        try:
            self.query_one(MiddlePanel).update(_render_picker(self._active_picker))
        except Exception:  # noqa: BLE001
            pass

    def _close_picker(self, applied: str | None = None) -> None:
        self._active_picker = None
        try:
            self.query_one(MiddlePanel).clear()
        except Exception:  # noqa: BLE001
            pass
        self._do_set_status("idle")
        if applied and self._on_model_swap is not None:
            # Picker selections persist by default — matches /model <id>
            # semantics and the operator-feedback "lock it in" intent.
            self._on_model_swap(applied, True)
            self._do_set_model(applied)
            self.write_top(f"[/model] switched to {applied} (persisted)", kind="system")
        # Hand focus back to the input line so the operator can keep typing.
        try:
            self.query_one(Input).focus()
        except Exception:  # noqa: BLE001
            pass

    async def action_cancel(self) -> None:
        if self._active_ask is not None:
            self._resolve_ask("__cancelled__")
            return
        if self._active_picker is not None:
            self._close_picker(None)
            self.write_top("[/model] cancelled", kind="system")
            return
        self._on_cancel()
        self._do_set_status("idle")

    async def action_quit(self) -> None:
        """Ctrl-D handler with two-press confirmation.

        First press arms; status bar shows the hint. Second press within
        QUIT_ARM_WINDOW_SEC exits cleanly. Anything else disarms.
        """
        now = time.monotonic()
        if self._quit_armed_at is not None and (now - self._quit_armed_at) <= self.QUIT_ARM_WINDOW_SEC:
            self.exit()
            return
        self._quit_armed_at = now
        self.write_top(
            f"[press Ctrl-D again within {int(self.QUIT_ARM_WINDOW_SEC)}s to quit · or type /quit]",
            kind="system",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _abbreviate_cwd(cwd: str, max_len: int = 28) -> str:
    """Shorten a path for the status row.

    /home/agent → ~
    /home/agent/projects/x → ~/projects/x
    /usr/share/very/deep/nested/path/here → /usr/share/…/path/here
    """
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
    # Middle-ellipsis: keep first segment + last 2 segments.
    parts = cwd.split("/")
    if len(parts) <= 3:
        # Path is genuinely just deep components, not many segments.
        keep = max_len // 2 - 1
        return cwd[:keep] + "…" + cwd[-(max_len - keep - 1):]
    head = parts[0] if parts[0] else "/"  # absolute path keeps leading /
    if not parts[0]:
        head = "/" + parts[1]
        tail = "/".join(parts[-2:])
    else:
        # Starts with ~ or relative
        tail = "/".join(parts[-2:])
    candidate = f"{head}/…/{tail}"
    if len(candidate) <= max_len:
        return candidate
    # Worst case: brutally truncate the tail.
    return candidate[:max_len - 1] + "…"
