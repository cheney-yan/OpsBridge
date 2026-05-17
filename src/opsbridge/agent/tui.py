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

  /model            switch model for this session (picker — phase-3 §11)
  /model <id>       switch directly to <id>
  /quit /exit /q    end the session
  /help /?          show this help

Direct exec:
  !<cmd>            run <cmd> via bash, skipping the LLM
                    (e.g. !tail -f /var/log/syslog)
                    audit chain still fires; no ask form.

Hotkeys:
  Ctrl-D ×2         quit (first press arms; second within 2s exits)
  Ctrl-C            cancel current ask form / running bash

Layout:
  status row        ◐ state · elapsed · cwd · @host · model · ctx%
                    fields drop right→left under width pressure.
                    ctx turns yellow ≥80%, red ≥90%.

Audit log:
  /var/log/opsbridge/agent/<session-id>.jsonl

Preferences:
  /etc/opsbridge/agent/preferences.md  (mutate only via `remember`)
"""


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class OpsBridgeApp(App):
    """Three-region TUI: top / middle / status+input."""

    CSS = """
    Screen { layout: vertical; }
    /* No borders anywhere — regions distinguished by background. */
    #top_log {
        height: 1fr;
        background: $surface;
        padding: 0 1;
    }
    #middle {
        height: auto;
        max-height: 30%;
        background: $boost;
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
        background: $surface;
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

    def __init__(
        self,
        *,
        hostname: str,
        model_label: str,
        on_operator_turn: Callable[[str], None],
        on_cancel: Callable[[], None],
        on_direct_bash: Callable[[str], None] | None = None,
        title: str = "OpsBridge",
    ) -> None:
        super().__init__()
        self._hostname = hostname
        self._model_label = model_label
        self._on_operator_turn = on_operator_turn
        self._on_cancel = on_cancel
        self._on_direct_bash = on_direct_bash
        # Window title (visible in tmux/terminal title bar). The on-screen
        # equivalent (hostname/model) lives in the consolidated status row.
        self.title = f"{title} · {hostname} · {model_label}"
        self._active_ask: _AskState | None = None
        self._spinner_task: asyncio.Task | None = None
        self._quit_armed_at: float | None = None
        self._state_start_time: float | None = None

    # ----- composition ----------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical():
            yield TopLog(id="top_log", wrap=True, highlight=False, markup=False)
            yield MiddlePanel(id="middle")
            yield StatusBar(id="status")
        yield Input(id="prompt", placeholder="ask the agent — /help for commands")

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

    def write_top(self, line: str) -> None:
        """Append a line to the top region. Safe from any thread."""
        try:
            self.call_from_thread(self._do_write_top, line)
        except RuntimeError:
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

    # ----- input ----------------------------------------------------------

    async def on_input_submitted(self, message: Input.Submitted) -> None:
        text = message.value.rstrip()
        message.input.value = ""
        if not text.strip():
            return
        # Any keystroke past the arm window disarms quit.
        self._quit_armed_at = None

        # Slash commands handled in-process — no LLM round-trip.
        stripped = text.strip()
        if stripped in ("/quit", "/exit", "/q"):
            self.exit()
            return
        if stripped in ("/help", "/?"):
            self._do_write_top(f"> {stripped}")
            for line in HELP_TEXT.splitlines():
                self._do_write_top(line)
            return

        # `!` prefix → direct bash, skip the LLM (phase-3 §12).
        # `\!` escapes the sigil: treated as plain English.
        if stripped.startswith("\\!"):
            text = stripped[1:]   # strip the backslash; route as English
        elif stripped.startswith("!"):
            cmd = stripped[1:].strip()
            if not cmd:
                return
            self._do_write_top(f"! {cmd}")
            if self._on_direct_bash is not None:
                self._on_direct_bash(cmd)
            return

        # Echo + hand to the agent thread.
        self._do_write_top(f"> {text}")
        self._do_set_final_answer("")
        self._do_set_status("thinking")
        self._on_operator_turn(text)

    async def action_cancel(self) -> None:
        if self._active_ask is not None:
            self._resolve_ask("__cancelled__")
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
        self._do_write_top(
            f"[press Ctrl-D again within {int(self.QUIT_ARM_WINDOW_SEC)}s to quit · or type /quit]"
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
