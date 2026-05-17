"""Per-role widgets for the §16 stream UI (PRD-phase3 §16).

Each conversation event mounts an instance of one of these widgets into
the `VerticalScroll #stream`. Widgets render their own content via rich
/ textual primitives — no hand-rolled ASCII-art rendering.

Class inventory:
  Append-only stream entries (subclass `_StreamMessage(Static)`):
    UserMessage, AssistantMessage, ToolCallMessage, ToolResultMessage,
    BashOutputLine, SystemNotice, ErrorMessage
  Transient widgets (mount → frozen / removed):
    ResponseStatus  — textual LoadingIndicator + elapsed label
    AskForm         — destructive-action confirmation form
    ModelPicker     — paginated /model switcher
  Operator input:
    PromptInput     — TextArea subclass, Enter submits / Shift+Enter newlines
"""
from __future__ import annotations

from typing import Callable

from rich.markdown import Markdown
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Label, LoadingIndicator, Static, TextArea


# ---------------------------------------------------------------------------
# Per-role stream entries
# ---------------------------------------------------------------------------

class _StreamMessage(Static):
    """Base for append-only stream entries.

    Subclasses override `render_text()` to produce the prefixed payload.
    Foreground color and any per-role styling lives in DEFAULT_CSS.
    """

    DEFAULT_CSS = """
    _StreamMessage {
        height: auto;
        width: 1fr;
        padding: 0 1;
    }
    """

    def __init__(self, text: str = "", **kwargs) -> None:
        # markup=False — payload text may contain `[…]` (e.g. `[/model]`,
        # `[help]`) which Rich would otherwise parse as markup and raise
        # MarkupError. Per-role color comes from DEFAULT_CSS.
        super().__init__(markup=False, **kwargs)
        self.text = text

    def on_mount(self) -> None:
        self.update(self.render_text())

    def render_text(self) -> str:  # pragma: no cover — overridden
        return self.text


class UserMessage(_StreamMessage):
    """`> <text>` — operator input echoed into the stream.

    Visual anchor: full-row dim background so the eye can locate each
    operator turn when scrolling back through history.
    """

    DEFAULT_CSS = """
    UserMessage {
        background: #1f2733;
        color: $text;
        padding: 0 1;
        margin: 1 0 0 0;
        text-style: bold;
    }
    """

    def render_text(self) -> str:
        return f"> {self.text}"


class AssistantMessage(_StreamMessage):
    """AI text / final-answer rendering. Goes through rich's Markdown
    so the model's lists, code-fences, and inline emphasis render.
    """

    DEFAULT_CSS = """
    AssistantMessage {
        padding: 1 1 0 1;
    }
    """

    def render_text(self) -> str:
        return self.text

    def on_mount(self) -> None:
        # Use the markdown renderable. Safe even pre-mount; Static.update
        # accepts a rich Renderable.
        self.update(Markdown(self.text or ""))


class ToolCallMessage(_StreamMessage):
    """`● Tool(args)` — compact tool-invocation summary, orange glyph."""

    DEFAULT_CSS = """
    ToolCallMessage { color: #ff8800; }
    """

    def __init__(self, tool: str = "", args_summary: str = "", **kwargs) -> None:
        super().__init__(text="", **kwargs)
        self.tool = tool
        self.args_summary = args_summary

    def render_text(self) -> str:
        if self.args_summary:
            return f"● {self.tool}({self.args_summary})"
        return f"● {self.tool}"


class ToolResultMessage(_StreamMessage):
    """`  ⎿  <summary>` — corner glyph, dim, two-space indent."""

    DEFAULT_CSS = """
    ToolResultMessage { color: $text-muted; }
    """

    def render_text(self) -> str:
        return f"  ⎿  {self.text}"


class BashOutputLine(_StreamMessage):
    """Raw streamed bash stdout/stderr line. Indented under the parent
    ToolCallMessage.
    """

    DEFAULT_CSS = """
    BashOutputLine { color: $text; }
    """

    def render_text(self) -> str:
        return f"     {self.text}"


class SystemNotice(_StreamMessage):
    """`※ <text>` — muted yellow, for /help, queue notices, transcripts."""

    DEFAULT_CSS = """
    SystemNotice { color: yellow; }
    """

    def render_text(self) -> str:
        return f"※ {self.text}"


class ErrorMessage(_StreamMessage):
    """`● <text>` red — errors, bash_post_kill surfacing, tool exceptions."""

    DEFAULT_CSS = """
    ErrorMessage { color: red; }
    """

    def render_text(self) -> str:
        return f"● {self.text}"


# ---------------------------------------------------------------------------
# ResponseStatus — transient "thinking" indicator at the stream tail
# ---------------------------------------------------------------------------

class ResponseStatus(Vertical):
    """Mounts at the tail of the stream while the agent is thinking.

    Composed of a Label (`✻ Thinking…`) + textual's built-in
    LoadingIndicator. Removed via App.end_thinking() on turn end.
    """

    DEFAULT_CSS = """
    ResponseStatus {
        height: 2;
        padding: 0 1;
        color: $text-muted;
    }
    ResponseStatus LoadingIndicator {
        height: 1;
        background: transparent;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("✻ Thinking…")
        yield LoadingIndicator()


# ---------------------------------------------------------------------------
# AskForm — inline confirmation widget
# ---------------------------------------------------------------------------

class AskForm(Widget, can_focus=True):
    """Destructive-action confirmation. Mounts inline, takes focus.

    On resolve: `frozen = True`, the widget stays in the stream as
    history and ignores further keys.
    """

    DEFAULT_CSS = """
    AskForm {
        height: auto;
        width: 1fr;
        padding: 1 2;
        border: round $warning;
        margin: 1 0;
    }
    AskForm:focus {
        border: round $accent;
    }
    AskForm.-frozen {
        border: round $surface;
        color: $text-muted;
    }
    """

    selected_idx: reactive[int] = reactive(0)
    frozen: reactive[bool] = reactive(False)

    def __init__(
        self,
        prompt: str,
        options: list[str],
        on_resolve: Callable[[str], None],
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.prompt = prompt
        self.options = list(options)
        self._on_resolve = on_resolve

    def render(self) -> str:
        lines = [f"▶ {self.prompt}", ""]
        for i, opt in enumerate(self.options):
            marker = "•" if i == self.selected_idx else " "
            default = " ← default" if i == 0 and i != self.selected_idx else ""
            lines.append(f"  ({marker}) {opt}{default}")
        lines.append("")
        if self.frozen:
            lines.append(f"✓ {self.options[self.selected_idx]}")
        else:
            lines.append("[arrows/Y/N to select, Enter to confirm, Ctrl-C to cancel]")
        return "\n".join(lines)

    def on_key(self, event) -> None:
        if self.frozen:
            return
        key = event.key
        if key in ("up", "left"):
            self.selected_idx = (self.selected_idx - 1) % len(self.options)
            event.stop()
            return
        if key in ("down", "right"):
            self.selected_idx = (self.selected_idx + 1) % len(self.options)
            event.stop()
            return
        if key.lower() == "y":
            for i, o in enumerate(self.options):
                if o.lower().startswith("y"):
                    self.selected_idx = i
                    break
            event.stop()
            return
        if key.lower() == "n":
            for i, o in enumerate(self.options):
                if o.lower().startswith("n"):
                    self.selected_idx = i
                    break
            event.stop()
            return
        if key == "enter":
            self.resolve(self.options[self.selected_idx])
            event.stop()
            return

    def resolve(self, choice: str) -> None:
        if self.frozen:
            return
        for i, o in enumerate(self.options):
            if o == choice:
                self.selected_idx = i
                break
        self.frozen = True
        self.add_class("-frozen")
        self.refresh()
        self._on_resolve(choice)


# ---------------------------------------------------------------------------
# ModelPicker — inline paginated picker
# ---------------------------------------------------------------------------

class ModelPicker(Widget, can_focus=True):
    """Inline /model picker. Mounts in the stream, takes focus, freezes on pick."""

    DEFAULT_CSS = """
    ModelPicker {
        height: auto;
        width: 1fr;
        padding: 1 2;
        border: round $accent;
        margin: 1 0;
    }
    ModelPicker.-frozen {
        border: round $surface;
        color: $text-muted;
    }
    """

    selected_idx: reactive[int] = reactive(0)
    page: reactive[int] = reactive(0)
    frozen: reactive[bool] = reactive(False)

    def __init__(
        self,
        models: list[str],
        *,
        current_model: str = "",
        page_size: int = 10,
        on_pick: Callable[[str], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.models = list(models)
        self.current_model = current_model
        self.page_size = page_size
        self._on_pick = on_pick
        self._on_cancel = on_cancel
        # Seed selection to the currently-active model.
        for i, m in enumerate(self.models):
            if m == current_model:
                self.selected_idx = i
                self.page = i // self.page_size
                break

    @property
    def total_pages(self) -> int:
        return max(1, (len(self.models) + self.page_size - 1) // self.page_size)

    def page_slice(self) -> tuple[int, int]:
        start = self.page * self.page_size
        end = min(start + self.page_size, len(self.models))
        return start, end

    def render(self) -> str:
        if not self.models:
            return "▶ Switch model\n\n  (no models discovered — type `/model <id>`)"
        lines = ["▶ Switch model (Enter applies, Esc cancels):", ""]
        start, end = self.page_slice()
        for idx_in_page, abs_idx in enumerate(range(start, end)):
            mid = self.models[abs_idx]
            marker = "•" if abs_idx == self.selected_idx else " "
            suffix = " ← current" if mid == self.current_model else ""
            num = idx_in_page + 1
            lines.append(f"  ({marker}) [{num}] {mid}{suffix}")
        lines.append("")
        if self.total_pages > 1:
            lines.append(
                f"page {self.page + 1}/{self.total_pages}  (n=next, p=prev, "
                f"1-{min(self.page_size, end - start)}=pick, Esc=cancel)"
            )
        else:
            lines.append("(1-9 to pick directly, Enter to apply selection, Esc to cancel)")
        if self.frozen:
            lines.append("")
            lines.append(f"✓ {self.models[self.selected_idx]}")
        return "\n".join(lines)

    def on_key(self, event) -> None:
        if self.frozen or not self.models:
            return
        key = event.key
        if key == "escape":
            self._cancel()
            event.stop()
            return
        if key == "enter":
            self._pick(self.models[self.selected_idx])
            event.stop()
            return
        if key == "up":
            self.selected_idx = (self.selected_idx - 1) % len(self.models)
            self.page = self.selected_idx // self.page_size
            event.stop()
            return
        if key == "down":
            self.selected_idx = (self.selected_idx + 1) % len(self.models)
            self.page = self.selected_idx // self.page_size
            event.stop()
            return
        if key.lower() == "n":
            self.page_relative(1)
            event.stop()
            return
        if key.lower() == "p":
            self.page_relative(-1)
            event.stop()
            return
        if key.isdigit() and key != "0":
            idx_in_page = int(key) - 1
            start, end = self.page_slice()
            target = start + idx_in_page
            if target < end:
                self._pick(self.models[target])
            event.stop()
            return

    def page_relative(self, delta: int) -> None:
        new_page = max(0, min(self.total_pages - 1, self.page + delta))
        if new_page != self.page:
            self.page = new_page
            self.selected_idx = self.page * self.page_size

    def _pick(self, mid: str) -> None:
        for i, m in enumerate(self.models):
            if m == mid:
                self.selected_idx = i
                break
        self.frozen = True
        self.add_class("-frozen")
        self.refresh()
        if self._on_pick is not None:
            self._on_pick(mid)

    def _cancel(self) -> None:
        if self._on_cancel is not None:
            self._on_cancel()


# ---------------------------------------------------------------------------
# PromptInput — TextArea subclass; Enter submits, Shift+Enter newlines
# ---------------------------------------------------------------------------

class PromptInput(TextArea):
    """Multi-line markdown-aware operator input.

    - Enter submits (posts `PromptInput.Submitted`).
    - Shift+Enter inserts a literal newline.
    - Rounded border with title / subtitle hint chrome.
    """

    DEFAULT_CSS = """
    PromptInput {
        height: auto;
        max-height: 8;
        border: round $accent;
    }
    """

    class Submitted(Message):
        """Fired when the operator presses Enter (without Shift)."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, **kwargs) -> None:
        super().__init__(language="markdown", **kwargs)

    def on_mount(self) -> None:
        self.border_title = "ask the agent — /help, !cmd, /model"
        self.border_subtitle = (
            "enter send · shift+enter newline · ^c clear · ^d×2 quit"
        )

    async def _on_key(self, event) -> None:
        # Intercept Enter / Shift+Enter BEFORE TextArea's default key
        # handling. Without prevent_default() the parent class swallows
        # Enter as a newline-insert.
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self.submit()
            return
        if event.key == "shift+enter":
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        await super()._on_key(event)

    def submit(self) -> None:
        text = self.text
        if text.strip() == "":
            return
        self.clear()
        self.post_message(self.Submitted(text))
