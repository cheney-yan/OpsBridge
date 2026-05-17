"""Four tools — read, write, bash, remember.

Per CLAUDE.md design discipline: tools stay dumb. Conflict detection,
risk assessment, confirmation prompts — that's all in the system prompt
in core.py. These functions only enforce structural invariants
(size caps, format, duplicate rejection) and emit audit events.
"""
from __future__ import annotations

import difflib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Iterator, Literal

from smolagents import Tool

# ---------------------------------------------------------------------------
# ANSI sanitizer (PRD §5)
# ---------------------------------------------------------------------------
# Keep CSI SGR sequences (`ESC [ ... m`) — colors / bold.
# Strip everything else: CSI cursor/screen control, OSC, ESC singles.

_CSI = r"\x1b\["
# SGR-keep: CSI parameters that end with 'm'. We DO NOT strip these.
# Everything else CSI-shaped gets stripped.
_CSI_STRIP = re.compile(rf"{_CSI}[0-?]*[ -/]*[@-ln-~]")
# OSC: ESC ] ... BEL or ESC ] ... ESC \  — terminal title etc.
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# ESC singles: ESC followed by one of @A-Z\\\]^_a-z{|}~ (excluding [ and ]).
_ESC_SINGLE = re.compile(r"\x1b[@-Z\\^_a-z{|}~]")
# Other rogue control bytes we don't want leaking (keep \t \n \r, and \x1b
# which only survives now as part of a passed-through SGR sequence).
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f]")


def sanitize(text: str) -> str:
    """One-pass sanitizer: keep SGR, strip everything else dangerous."""
    if not text:
        return text
    # 1. Strip OSC first (it can contain ; and other chars that confuse CSI matching).
    text = _OSC.sub("", text)
    # 2. CSI: strip if it does NOT end in 'm' (SGR).
    def _csi_repl(m: re.Match) -> str:
        seq = m.group(0)
        return seq if seq.endswith("m") else ""
    text = _CSI_STRIP.sub(_csi_repl, text)
    # 3. ESC singles — save/restore cursor, charset switch, etc.
    text = _ESC_SINGLE.sub("", text)
    # 4. Other control bytes (NUL, BEL, etc.).
    text = _CTRL.sub("", text)
    return text


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------

DEFAULT_READ_LIMIT = 2000
MAX_LINE_LEN = 2000


def _format_lines(start_lineno: int, raw: str) -> str:
    """Number lines like `cat -n` for the LLM."""
    out: list[str] = []
    for i, line in enumerate(raw.splitlines()):
        if len(line) > MAX_LINE_LEN:
            line = line[:MAX_LINE_LEN] + f"…[line truncated at {MAX_LINE_LEN} chars]"
        out.append(f"{start_lineno + i:6d}\t{line}")
    return "\n".join(out)


def tool_read(path: str, offset: int = 0, limit: int = DEFAULT_READ_LIMIT) -> str:
    """Read a file. Line-numbered output.

    Args:
        path: absolute or relative path.
        offset: 0-indexed line offset.
        limit: max number of lines to return.
    """
    p = Path(path)
    if not p.exists():
        return f"[read error: file not found: {path}]"
    if p.is_dir():
        return f"[read error: is a directory: {path}]"
    try:
        # Binary detection: if we can't decode as utf-8/latin-1, fall back to repr.
        with open(p, "rb") as fh:
            data = fh.read()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1", errors="replace")
    except OSError as exc:
        return f"[read error: {exc}]"

    text = sanitize(text)
    lines = text.splitlines()
    total = len(lines)
    if offset < 0:
        offset = 0
    end = min(offset + limit, total)
    if offset >= total:
        return f"[file has {total} lines; offset {offset} is past end]"
    window = "\n".join(lines[offset:end])
    header = (
        f"[showing lines {offset + 1}..{end} of {total}]\n"
        if (offset > 0 or end < total)
        else ""
    )
    return header + _format_lines(offset + 1, window)


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------

def tool_write(path: str, content: str) -> str:
    """Write/overwrite a file. Does NOT create parent dirs unless they exist."""
    p = Path(path)
    if not p.parent.exists():
        return (
            f"[write error: parent directory does not exist: {p.parent} — "
            "run `mkdir -p` via bash first if you want to create it]"
        )
    try:
        # Atomic-ish write: tmp + rename in the same directory.
        with tempfile.NamedTemporaryFile(
            "w", dir=p.parent, delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        os.replace(tmp_path, p)
    except OSError as exc:
        try:
            os.unlink(tmp_path)
        except (OSError, NameError):
            pass
        return f"[write error: {exc}]"
    return f"[wrote {len(content)} chars to {path}]"


# ---------------------------------------------------------------------------
# bash
# ---------------------------------------------------------------------------

DEFAULT_BASH_TIMEOUT = 60
DEFAULT_BASH_CWD = "/home/agent"
ROLLING_WINDOW_LINES = 5


def _stream_with_rolling_window(
    proc: subprocess.Popen,
    tui_writer: Callable[[str], None] | None,
    is_tty: bool,
) -> str:
    """Stream proc stdout to the TUI as a rolling 5-line window; collect all bytes.

    Returns the full captured output (sanitized).
    """
    captured: list[str] = []
    window: list[str] = []

    def render_window() -> None:
        if not is_tty or tui_writer is None:
            return
        # Clear previous window: move up N lines, clear-to-end-of-screen.
        # We use SGR-allowed codes; cursor controls would be stripped by our
        # own sanitizer if we ran them through it, so write directly to fd.
        prev_lines = render_window.prev_lines  # type: ignore[attr-defined]
        if prev_lines:
            tui_writer(f"\x1b[{prev_lines}F\x1b[J")
        for line in window:
            tui_writer(line + "\n")
        render_window.prev_lines = len(window)  # type: ignore[attr-defined]

    render_window.prev_lines = 0  # type: ignore[attr-defined]

    assert proc.stdout is not None
    buf = ""
    while True:
        chunk = proc.stdout.read(4096)
        if not chunk:
            break
        text = chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace")
        text = sanitize(text)
        buf += text
        captured.append(text)
        if "\n" in buf:
            parts = buf.split("\n")
            new_lines, buf = parts[:-1], parts[-1]
            for line in new_lines:
                if is_tty and tui_writer is not None:
                    window.append(line)
                    if len(window) > ROLLING_WINDOW_LINES:
                        window.pop(0)
                    render_window()
                elif tui_writer is not None:
                    tui_writer(line + "\n")

    # Any partial trailing line.
    if buf:
        if is_tty and tui_writer is not None:
            window.append(buf)
            if len(window) > ROLLING_WINDOW_LINES:
                window.pop(0)
            render_window()
        elif tui_writer is not None:
            tui_writer(buf + "\n")

    return "".join(captured)


def tool_bash(
    command: str,
    timeout_sec: int = DEFAULT_BASH_TIMEOUT,
    *,
    tui_writer: Callable[[str], None] | None = None,
    is_tty: bool = False,
    cwd: str = DEFAULT_BASH_CWD,
) -> tuple[str, dict]:
    """Run a shell command via `bash -lc`.

    Captures stdout+stderr merged. Returns the captured output (sanitized)
    plus a metadata dict (exit code, duration, timeout flag).
    """
    # Default cwd may not exist on dev machines — fall back to user $HOME.
    if not Path(cwd).exists():
        cwd = os.path.expanduser("~")

    started = time.monotonic()
    timed_out = False

    # bash -lc → login shell sources ~/.profile (PRD §6 credential model).
    try:
        proc = subprocess.Popen(
            ["bash", "-lc", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            text=True,
            bufsize=1,
            start_new_session=True,  # so we can kill the whole process group on timeout
        )
    except OSError as exc:
        return f"[bash error: {exc}]", {"exit": -1, "duration_ms": 0, "timeout": False}

    # Watchdog that kills the process group on timeout.
    def _killer() -> None:
        nonlocal timed_out
        if proc.wait(timeout=timeout_sec) is None:
            return  # already exited

    # Simpler: poll loop in main thread while a background thread reads stdout.
    output_holder: dict[str, str] = {}
    stream_exc: dict[str, BaseException] = {}

    def _drain() -> None:
        try:
            output_holder["data"] = _stream_with_rolling_window(
                proc, tui_writer, is_tty
            )
        except BaseException as exc:  # noqa: BLE001
            stream_exc["err"] = exc

    t = threading.Thread(target=_drain, daemon=True)
    t.start()

    try:
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        timed_out = True
        # SIGTERM the whole process group, then SIGKILL after 2s grace.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass

    t.join(timeout=2)
    duration_ms = int((time.monotonic() - started) * 1000)

    output = output_holder.get("data", "")
    if timed_out:
        output += f"\n[timeout after {timeout_sec}s]"

    return output, {
        "exit": proc.returncode if proc.returncode is not None else -1,
        "duration_ms": duration_ms,
        "timeout": timed_out,
    }


# ---------------------------------------------------------------------------
# remember
# ---------------------------------------------------------------------------

PREFS_PATH = Path("/etc/opsbridge/agent/preferences.md")
PREFS_MAX_LINES = 50
PREFS_MAX_BYTES = 4096
PREFS_HEADER_LINE = "# Operator preferences"


def _read_prefs(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _write_prefs_via_sudo(path: Path, content: str, *, created_event: bool) -> None:
    """Write the preferences file with mode 0640 root:agent.

    Uses sudo + tee so the agent user (which doesn't own /etc/opsbridge/agent/)
    can update it. In tests we override PREFS_PATH so this path is rare.
    """
    # Write to a temp file we own, then sudo cp + chown + chmod.
    with tempfile.NamedTemporaryFile(
        "w", delete=False, encoding="utf-8", suffix=".prefs"
    ) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        if not path.parent.exists():
            subprocess.run(
                ["sudo", "-n", "mkdir", "-p", str(path.parent)],
                check=True,
                capture_output=True,
            )
        subprocess.run(
            ["sudo", "-n", "cp", tmp_path, str(path)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["sudo", "-n", "chown", "root:agent", str(path)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["sudo", "-n", "chmod", "0640", str(path)],
            check=True,
            capture_output=True,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _write_prefs_direct(path: Path, content: str) -> None:
    """Write directly (used when the caller already has permission, e.g. tests)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _bullet_normalize(content: str) -> str:
    """Coerce 'foo' or '* foo' to '- foo'. Strip trailing whitespace."""
    s = content.strip()
    if not s:
        return ""
    if s.startswith(("- ", "* ")):
        s = "- " + s[2:].strip()
    elif s.startswith("-") or s.startswith("*"):
        s = "- " + s[1:].strip()
    else:
        s = "- " + s
    return s


def _diff(before: str, after: str, path: Path) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path.name}",
            tofile=f"b/{path.name}",
        )
    )


def tool_remember(
    action: Literal["add", "remove"],
    content: str,
    *,
    prefs_path: Path = PREFS_PATH,
    writer: Callable[[Path, str, bool], None] | None = None,
    audit: Callable[[str, dict], None] | None = None,
) -> str:
    """The sole sanctioned path for mutating preferences.md.

    Structural invariants only — judgment is the LLM's job per system prompt.
    """
    action = action.lower().strip()  # type: ignore[assignment]
    if action not in ("add", "remove"):
        return f"[remember error: action must be 'add' or 'remove', got {action!r}]"

    bullet = _bullet_normalize(content)
    if not bullet or bullet == "- ":
        return "[remember error: content is empty]"

    before = _read_prefs(prefs_path)
    file_existed_before = prefs_path.exists()

    if action == "remove":
        if not file_existed_before:
            # PRD: silent no-op for remove against missing file.
            if audit is not None:
                audit("preferences_mutation", {
                    "action": "remove",
                    "noop_reason": "missing_file",
                    "content": bullet,
                })
            return "[remember: preferences file does not exist; nothing to remove]"
        lines = before.splitlines()
        bullet_text = bullet[2:].strip()  # strip "- "
        new_lines = []
        removed = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(("- ", "* ")):
                line_text = stripped[2:].strip()
                if not removed and line_text == bullet_text:
                    removed = True
                    continue
            new_lines.append(line)
        if not removed:
            return f"[remember error: bullet not found: {bullet_text!r}]"
        after = "\n".join(new_lines)
        if after and not after.endswith("\n"):
            after += "\n"

    else:  # add
        # Size discipline: hard cap at 50 lines / 4 KB.
        existing_bullets = [
            ln.strip() for ln in before.splitlines() if ln.strip().startswith(("- ", "* "))
        ]
        bullet_text = bullet[2:].strip()
        for eb in existing_bullets:
            if eb[2:].strip() == bullet_text:
                return f"[remember error: duplicate bullet: {bullet_text!r}]"
        # Build the candidate file.
        if before.strip():
            candidate = before.rstrip("\n") + "\n" + bullet + "\n"
        else:
            candidate = f"{PREFS_HEADER_LINE}\n\n{bullet}\n"
        cand_lines = candidate.splitlines()
        if len(cand_lines) > PREFS_MAX_LINES:
            return (
                f"[remember error: would exceed {PREFS_MAX_LINES}-line cap "
                f"({len(cand_lines)} lines after add) — prune first]"
            )
        if len(candidate.encode("utf-8")) > PREFS_MAX_BYTES:
            return (
                f"[remember error: would exceed {PREFS_MAX_BYTES}-byte cap — prune first]"
            )
        after = candidate

    if before == after:
        return "[remember: no change]"

    diff_text = _diff(before, after, prefs_path)

    # Write. Picker: if the file is writable directly by us, do that;
    # otherwise go via sudo.
    _writer = writer
    if _writer is None:
        # Decide direct vs sudo. Test code typically passes its own writer.
        if file_existed_before and os.access(prefs_path, os.W_OK):
            _writer = lambda p, c, _created: _write_prefs_direct(p, c)
        elif not file_existed_before and os.access(prefs_path.parent, os.W_OK):
            _writer = lambda p, c, _created: _write_prefs_direct(p, c)
        else:
            _writer = lambda p, c, _created: _write_prefs_via_sudo(p, c, created_event=_created)

    _writer(prefs_path, after, not file_existed_before)

    if audit is not None:
        if not file_existed_before:
            audit("preferences_file_created", {"path": str(prefs_path)})
        audit(
            "preferences_mutation",
            {"action": action, "content": bullet, "diff": diff_text},
        )

    verb = "added" if action == "add" else "removed"
    return f"[remember: {verb} bullet {bullet[2:].strip()!r}]"


# ---------------------------------------------------------------------------
# smolagents Tool wrappers
# ---------------------------------------------------------------------------
# smolagents wants Tool subclasses with a `forward` method, but it also
# accepts decorator-built tools. We build subclasses so we can inject a
# per-session SessionLogger and TUI writer.


class ReadTool(Tool):
    name = "read"
    description = (
        "Read a UTF-8 text file. Output is line-numbered like `cat -n`. "
        "Use `offset` and `limit` to paginate large files (default limit=2000)."
    )
    inputs = {
        "path": {"type": "string", "description": "Path to the file."},
        "offset": {"type": "integer", "description": "0-indexed line offset.", "nullable": True},
        "limit": {"type": "integer", "description": "Max number of lines.", "nullable": True},
    }
    output_type = "string"

    def __init__(self, logger=None):
        super().__init__()
        self._logger = logger

    def forward(self, path: str, offset: int = 0, limit: int = DEFAULT_READ_LIMIT) -> str:
        t0 = time.monotonic()
        result = tool_read(path, offset=offset, limit=limit)
        if self._logger:
            self._logger.emit(
                "tool_call",
                tool="read",
                args={"path": path, "offset": offset, "limit": limit},
                result=result,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
        return result


class WriteTool(Tool):
    name = "write"
    description = (
        "Write (or overwrite) a UTF-8 text file. Parent dir must already exist "
        "— use `bash` with `mkdir -p` first if not."
    )
    inputs = {
        "path": {"type": "string", "description": "Path to the file."},
        "content": {"type": "string", "description": "Full file contents."},
    }
    output_type = "string"

    def __init__(self, logger=None):
        super().__init__()
        self._logger = logger

    def forward(self, path: str, content: str) -> str:
        t0 = time.monotonic()
        result = tool_write(path, content)
        if self._logger:
            self._logger.emit(
                "tool_call",
                tool="write",
                args={"path": path, "content": content},
                result=result,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
        return result


class BashTool(Tool):
    name = "bash"
    description = (
        "Run a shell command via `bash -lc` (login shell, sources ~/.profile). "
        "Captures merged stdout+stderr and returns it. Default timeout 60s; "
        "on timeout the subprocess is killed and partial output is returned "
        "with `[timeout after Ns]` appended."
    )
    inputs = {
        "command": {"type": "string", "description": "Shell command line."},
        "timeout_sec": {
            "type": "integer",
            "description": "Timeout in seconds (default 60).",
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(self, logger=None, tui_writer=None, is_tty: bool = False):
        super().__init__()
        self._logger = logger
        self._tui_writer = tui_writer
        self._is_tty = is_tty

    def forward(self, command: str, timeout_sec: int = DEFAULT_BASH_TIMEOUT) -> str:
        t0 = time.monotonic()
        result, meta = tool_bash(
            command,
            timeout_sec=timeout_sec,
            tui_writer=self._tui_writer,
            is_tty=self._is_tty,
        )
        if self._logger:
            self._logger.emit(
                "tool_call",
                tool="bash",
                args={"command": command, "timeout_sec": timeout_sec},
                result=result,
                duration_ms=meta["duration_ms"],
                exit=meta["exit"],
                timeout=meta["timeout"],
            )
        return result


class RememberTool(Tool):
    name = "remember"
    description = (
        "The sole sanctioned path for mutating the operator preferences file at "
        "/etc/opsbridge/agent/preferences.md. Use `action='add'` to append a "
        "bullet, `action='remove'` to delete one. Caller (the LLM) is "
        "responsible for asking the operator for confirmation BEFORE calling — "
        "the system prompt mandates this. Structural invariants enforced here: "
        "no duplicates, max 50 lines / 4 KB."
    )
    inputs = {
        "action": {
            "type": "string",
            "description": "Either 'add' or 'remove'.",
            "enum": ["add", "remove"],
        },
        "content": {
            "type": "string",
            "description": "The bullet content (without leading '- ').",
        },
    }
    output_type = "string"

    def __init__(self, logger=None, prefs_path: Path = PREFS_PATH):
        super().__init__()
        self._logger = logger
        self._prefs_path = prefs_path

    def forward(self, action: str, content: str) -> str:
        t0 = time.monotonic()

        def _audit(event: str, payload: dict) -> None:
            if self._logger:
                self._logger.emit(event, **payload)

        result = tool_remember(
            action,  # type: ignore[arg-type]
            content,
            prefs_path=self._prefs_path,
            audit=_audit,
        )
        if self._logger:
            self._logger.emit(
                "tool_call",
                tool="remember",
                args={"action": action, "content": content},
                result=result,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
        return result
