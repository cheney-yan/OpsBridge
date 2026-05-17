"""Seven tools — read, write, bash, search, visit, ask, remember.

Per CLAUDE.md design discipline: tools stay dumb. Conflict detection,
risk assessment, confirmation prompts — that's all in the system prompt
in prompts/system.md. These functions only enforce structural invariants
(size caps, format, duplicate rejection) and emit audit events.
"""
from __future__ import annotations

import difflib
import errno
import fcntl
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

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
    """Read a file. Line-numbered output."""
    p = Path(path)
    if not p.exists():
        return f"[read error: file not found: {path}]"
    if p.is_dir():
        return f"[read error: is a directory: {path}]"
    try:
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

# Phase 3 §13: sticky cwd. We wrap each command with a `; printf MARK + pwd
# + MARK` tail so we can scrape the post-command cwd from captured output
# and pass it back as the cwd for the NEXT bash call. Markers are unlikely
# to appear in real output and pass through the ANSI sanitiser unchanged.
_CWD_MARK_START = "__OPSBRIDGE_CWD_BEGIN__"
_CWD_MARK_END = "__OPSBRIDGE_CWD_END__"
_CWD_MARK_RE = re.compile(
    rf"\n?{re.escape(_CWD_MARK_START)}(.*?){re.escape(_CWD_MARK_END)}\n?",
    re.DOTALL,
)


def _wrap_cwd_capture(command: str) -> str:
    """Append a pwd-printing tail to the operator's command so we can scrape
    the new cwd out of the captured output.

    Uses `{ ... ; }` grouping so the cwd-print runs in the SAME shell as the
    command — `cd` inside the command persists for the printf. If the command
    `exit`s early or the wrapping is broken (unbalanced braces), the marker
    simply doesn't fire and the caller keeps the previous cwd.
    """
    # Preserve the original command's exit code via $? — printf runs AFTER
    # the command but musn't overwrite the operator-visible $?.
    return (
        f"{{ {command}\n}}\n_OB_RC=$?\n"
        f"printf '\\n%s%s%s\\n' '{_CWD_MARK_START}' \"$(pwd 2>/dev/null)\" '{_CWD_MARK_END}'\n"
        f"exit $_OB_RC"
    )


def _extract_cwd(output: str) -> tuple[str, str | None]:
    """Strip the cwd marker from captured output and return the captured cwd.

    Returns (cleaned_output, cwd_or_None). When no marker is present (early
    exit, parse hiccup), returns (output, None).
    """
    m = _CWD_MARK_RE.search(output)
    if not m:
        return output, None
    cwd = (m.group(1) or "").strip()
    cleaned = (_CWD_MARK_RE.sub("", output, count=1)).rstrip()
    if cleaned:
        cleaned += "\n"
    return cleaned, (cwd or None)


def _stream_lines(
    proc: subprocess.Popen,
    line_sink: Callable[[str], None] | None,
) -> str:
    """Pipe-mode reader: stream proc stdout line-by-line; capture all bytes.

    Kept for the test_track_cwd=False / non-PTY callers that want strictly
    unaltered subprocess behaviour. Default path is `_stream_lines_pty`.
    """
    captured: list[str] = []
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
            if line_sink is not None:
                for line in new_lines:
                    line_sink(line)
    if buf and line_sink is not None:
        line_sink(buf)
    return "".join(captured)


def _stream_lines_pty(
    master_fd: int,
    line_sink: Callable[[str], None] | None,
) -> str:
    """PTY-mode reader (phase-3 §1): read from PTY master until child closes.

    Linux signals child-side close with OSError(EIO); macOS gives an empty
    read. Both are handled identically as "subprocess is done". Output is
    normalised so \\r\\n → \\n (PTYs run in canonical mode by default and
    inject the carriage return on every line).
    """
    captured: list[str] = []
    buf = ""
    while True:
        try:
            chunk = os.read(master_fd, 4096)
        except OSError as exc:
            if exc.errno == errno.EIO:
                break
            raise
        if not chunk:
            break
        text = chunk.decode("utf-8", errors="replace")
        # PTY canonical-mode artefacts: CRLF → LF. Bare \r (progress-bar
        # carriage return) is left intact for installers that paint over
        # the same line; sanitize() strips it as a control byte.
        text = text.replace("\r\n", "\n")
        text = sanitize(text)
        buf += text
        captured.append(text)
        if "\n" in buf:
            parts = buf.split("\n")
            new_lines, buf = parts[:-1], parts[-1]
            if line_sink is not None:
                for line in new_lines:
                    line_sink(line)
    if buf and line_sink is not None:
        line_sink(buf)
    return "".join(captured)


def _set_pty_winsize(fd: int, rows: int = 40, cols: int = 120) -> None:
    """Tell the child PTY how big the terminal is.

    Default 40×120 — wide enough that progress bars and tabular output
    (`top`, `htop` if used) don't reflow into garbage. Doesn't need to
    match the operator's actual terminal because we re-flow output line-
    by-line into the top region anyway.
    """
    try:
        size = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
    except OSError:
        pass


def tool_bash(
    command: str,
    timeout_sec: int = DEFAULT_BASH_TIMEOUT,
    *,
    line_sink: Callable[[str], None] | None = None,
    cwd: str = DEFAULT_BASH_CWD,
    pre_exec: Callable[[], None] | None = None,
    track_cwd: bool = True,
    use_pty: bool = True,
    proc_sink: Callable[[subprocess.Popen], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[str, dict]:
    """Run a shell command via `bash -lc`.

    Captures stdout+stderr merged. Returns the captured output (sanitized)
    plus a metadata dict (exit code, duration, timeout flag, and — when
    `track_cwd=True` and the command runs to completion — the post-command
    cwd via `meta["cwd"]`).

    `pre_exec` fires *before* the subprocess starts. `proc_sink` receives
    the Popen handle right after spawn so callers (BashTool.cancel)
    can signal the process group asynchronously.

    `track_cwd` (§13): wrap the command with a pwd-capture tail so the
    sticky-cwd indicator follows `cd` across calls.

    `use_pty` (§1, default True): give the child a PTY for stdout/stderr.
    Children using C stdio detect the TTY and stay line-buffered, so
    install scripts (apt, curl|bash, node setup) stream output in real
    time instead of arriving in a single 4 KB block at the end.

    `cancel_event`: if set during execution, send SIGTERM to the process
    group; SIGKILL 2 s later if still running. Returns with the captured
    output so far plus a `cancelled=True` flag in `meta`.
    """
    if not Path(cwd).exists():
        cwd = os.path.expanduser("~")

    if pre_exec is not None:
        try:
            pre_exec()
        except Exception:  # noqa: BLE001
            pass

    effective_cmd = _wrap_cwd_capture(command) if track_cwd else command

    started = time.monotonic()
    timed_out = False
    cancelled = False
    master_fd: int | None = None
    slave_fd: int | None = None

    try:
        if use_pty:
            master_fd, slave_fd = pty.openpty()
            _set_pty_winsize(slave_fd)
            proc = subprocess.Popen(
                ["bash", "-lc", effective_cmd],
                stdin=subprocess.DEVNULL,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=cwd,
                close_fds=True,
                start_new_session=True,
            )
            os.close(slave_fd)
            slave_fd = None
        else:
            proc = subprocess.Popen(
                ["bash", "-lc", effective_cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=cwd,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
    except OSError as exc:
        if master_fd is not None:
            try: os.close(master_fd)
            except OSError: pass
        if slave_fd is not None:
            try: os.close(slave_fd)
            except OSError: pass
        return f"[bash error: {exc}]", {"exit": -1, "duration_ms": 0, "timeout": False}

    if proc_sink is not None:
        try:
            proc_sink(proc)
        except Exception:  # noqa: BLE001
            pass

    output_holder: dict[str, str] = {}

    def _drain() -> None:
        try:
            if use_pty and master_fd is not None:
                output_holder["data"] = _stream_lines_pty(master_fd, line_sink)
            else:
                output_holder["data"] = _stream_lines(proc, line_sink)
        except BaseException:  # noqa: BLE001
            pass

    t = threading.Thread(target=_drain, daemon=True)
    t.start()

    # Wait loop — supports timeout AND mid-flight cancellation. Poll the
    # cancel_event every 200ms while we wait so Ctrl-C feels responsive.
    deadline = started + timeout_sec
    poll_interval = 0.2

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        try:
            proc.wait(timeout=min(poll_interval, remaining))
            # Exited. If cancel() signalled us BEFORE the wait returned
            # (SIGTERM killed the proc instantly), attribute it to cancel
            # rather than natural exit so the audit log + return marker
            # reflect what really happened.
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
            break
        except subprocess.TimeoutExpired:
            continue

    # Escalation: SIGTERM → wait 2s → SIGKILL → wait 1s.
    if timed_out or cancelled:
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

    if master_fd is not None:
        try: os.close(master_fd)
        except OSError: pass

    duration_ms = int((time.monotonic() - started) * 1000)

    output = output_holder.get("data", "")

    # Extract cwd from the wrapped tail BEFORE any timeout suffix is appended.
    captured_cwd: str | None = None
    if track_cwd and not (timed_out or cancelled):
        output, captured_cwd = _extract_cwd(output)

    if cancelled:
        output += "\n[cancelled by operator]"
    elif timed_out:
        output += f"\n[timeout after {timeout_sec}s]"

    meta: dict = {
        "exit": proc.returncode if proc.returncode is not None else -1,
        "duration_ms": duration_ms,
        "timeout": timed_out,
        "cancelled": cancelled,
    }
    if captured_cwd:
        meta["cwd"] = captured_cwd
    return output, meta


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

SEARCH_MAX_RESULTS_CAP = 20


def _format_search_results(raw: Any) -> tuple[str, int]:
    """Coerce the smolagents WebSearchTool output into our stanza format.

    Newer smolagents returns a markdown string; older versions return a
    list of dicts with `title`/`href`/`body`. Handle both.
    """
    if isinstance(raw, str):
        # WebSearchTool emits a markdown-ish list — count rough results
        # by counting URL occurrences for the audit log.
        count = len(re.findall(r"https?://", raw))
        return raw, count

    if isinstance(raw, list):
        out: list[str] = []
        for i, r in enumerate(raw, start=1):
            if isinstance(r, dict):
                title = r.get("title") or r.get("name") or "(no title)"
                snippet = r.get("body") or r.get("snippet") or r.get("description") or ""
                url = r.get("href") or r.get("url") or r.get("link") or ""
            else:
                title, snippet, url = str(r), "", ""
            out.append(f"{i}. {title}\n   {snippet}\n   {url}")
        return "\n\n".join(out), len(raw)

    return str(raw), 0


def tool_search(
    query: str,
    max_results: int = 5,
    *,
    backend: Callable[[str], Any] | None = None,
) -> tuple[str, dict]:
    """Run a web search and return formatted results + metadata."""
    capped = max(1, min(int(max_results or 5), SEARCH_MAX_RESULTS_CAP))
    if backend is None:
        try:
            from smolagents import WebSearchTool  # type: ignore
            tool = WebSearchTool(max_results=capped)
            def _call(q: str) -> Any:
                return tool.forward(q)
            backend = _call
        except Exception as exc:  # noqa: BLE001
            return (
                f"[search error: backend unavailable: {exc}]",
                {"backend": "web_search", "result_count": 0, "max_results": capped, "error": str(exc)},
            )

    t0 = time.monotonic()
    try:
        raw = backend(query)
    except Exception as exc:  # noqa: BLE001
        return (
            f"[search error: {exc}]",
            {
                "backend": "web_search",
                "result_count": 0,
                "max_results": capped,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "error": str(exc),
            },
        )
    formatted, count = _format_search_results(raw)
    return formatted, {
        "backend": "web_search",
        "result_count": count,
        "max_results": capped,
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }


# ---------------------------------------------------------------------------
# visit
# ---------------------------------------------------------------------------

JINA_READER_BASE = "https://r.jina.ai/"
VISIT_DEFAULT_TIMEOUT_SEC = 15
VISIT_DEFAULT_MAX_BYTES = 50_000


def tool_visit(
    url: str,
    *,
    jina_api_key: str = "",
    timeout_sec: int = VISIT_DEFAULT_TIMEOUT_SEC,
    max_bytes: int = VISIT_DEFAULT_MAX_BYTES,
    httpx_client: Any = None,
) -> tuple[str, dict]:
    """Fetch a URL through Jina Reader and return rendered markdown.

    Single backend — see PRD-phase2.md §"Web access". No local fetch fallback.
    """
    target = url.strip()
    if not target:
        return "[visit error: empty url]", {"bytes": 0, "duration_ms": 0, "truncated": False, "status": -1}
    if not (target.startswith("http://") or target.startswith("https://")):
        return f"[visit error: url must start with http(s): {url!r}]", {
            "bytes": 0, "duration_ms": 0, "truncated": False, "status": -1,
        }

    proxied = JINA_READER_BASE + target
    headers: dict[str, str] = {"Accept": "text/markdown, text/plain;q=0.9, */*;q=0.5"}
    if jina_api_key:
        headers["Authorization"] = f"Bearer {jina_api_key}"

    t0 = time.monotonic()
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover — declared dependency
        return f"[visit error: httpx unavailable: {exc}]", {
            "bytes": 0, "duration_ms": 0, "truncated": False, "status": -1,
        }

    client_ctx = httpx_client if httpx_client is not None else httpx.Client(timeout=timeout_sec)
    close_client = httpx_client is None
    try:
        try:
            resp = client_ctx.get(proxied, headers=headers, timeout=timeout_sec)
        except httpx.TimeoutException:
            return f"[visit timeout after {timeout_sec}s]", {
                "url": target,
                "bytes": 0,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "truncated": False,
                "status": -1,
                "timeout": True,
            }
        except httpx.HTTPError as exc:
            return f"[visit error: {exc}]", {
                "url": target,
                "bytes": 0,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "truncated": False,
                "status": -1,
                "error": str(exc),
            }
    finally:
        if close_client:
            try:
                client_ctx.close()
            except Exception:  # noqa: BLE001
                pass

    body = resp.text or ""
    encoded = body.encode("utf-8", errors="replace")
    truncated = False
    if len(encoded) > max_bytes:
        body = encoded[:max_bytes].decode("utf-8", errors="replace")
        truncated = True
        body = body + "\n\n[truncated]"
    body = sanitize(body)
    meta = {
        "url": target,
        "bytes": len(encoded),
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "truncated": truncated,
        "status": resp.status_code,
    }
    if resp.status_code >= 400:
        return f"[visit error: HTTP {resp.status_code} from Jina Reader]\n{body[:1000]}", meta
    return body, meta


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
    """Write the preferences file with mode 0640 root:agent."""
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _bullet_normalize(content: str) -> str:
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
            if audit is not None:
                audit("preferences_mutation", {
                    "action": "remove",
                    "noop_reason": "missing_file",
                    "content": bullet,
                })
            return "[remember: preferences file does not exist; nothing to remove]"
        lines = before.splitlines()
        bullet_text = bullet[2:].strip()
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
        existing_bullets = [
            ln.strip() for ln in before.splitlines() if ln.strip().startswith(("- ", "* "))
        ]
        bullet_text = bullet[2:].strip()
        for eb in existing_bullets:
            if eb[2:].strip() == bullet_text:
                return f"[remember error: duplicate bullet: {bullet_text!r}]"
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

    _writer = writer
    if _writer is None:
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
        "with `[timeout after Ns]` appended. Live output streams to the operator's "
        "TUI top-region log."
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

    def __init__(self, logger=None, app=None, cwd: str = DEFAULT_BASH_CWD):
        super().__init__()
        self._logger = logger
        self._app = app
        # §13: sticky cwd. Starts at the install-time default; every
        # successful bash call updates it from the wrapped pwd capture.
        self._current_cwd = cwd
        # §3: operator-cancellation. Set by `cancel()`; the wait loop in
        # `tool_bash` polls it and SIGTERMs the process group when set.
        self._cancel_event = threading.Event()
        self._active_proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        if self._app is not None:
            try:
                self._app.set_cwd(cwd)
            except Exception:  # noqa: BLE001
                pass

    @property
    def current_cwd(self) -> str:
        return self._current_cwd

    @property
    def is_running(self) -> bool:
        """True iff a subprocess is currently active (for the cancel wiring)."""
        with self._proc_lock:
            return self._active_proc is not None

    def cancel(self) -> bool:
        """Signal the in-flight subprocess to terminate (phase-3 §3).

        Returns True if a process was signalled, False if no process was
        active. Safe to call from any thread.
        """
        with self._proc_lock:
            proc = self._active_proc
        if proc is None or proc.poll() is not None:
            return False
        self._cancel_event.set()
        # The wait loop in tool_bash will pick up the event and run the
        # SIGTERM→SIGKILL escalation. We also send SIGTERM immediately so
        # operators don't wait up to poll_interval (~0.2s) for the signal.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        return True

    def forward(self, command: str, timeout_sec: int = DEFAULT_BASH_TIMEOUT) -> str:
        """smolagents-facing entrypoint. LLM-routed by definition.

        Operator-direct (§12 `!`) calls bypass smolagents' tool dispatch and
        invoke `run_direct` directly, so the `source` field can vary without
        breaking smolagents' inputs-vs-signature validation.
        """
        return self._exec(command, timeout_sec, source="llm")

    def _exec(self, command: str, timeout_sec: int, *, source: str) -> str:
        """Shared exec path for both `forward` (LLM) and `run_direct` (operator).

        `source` distinguishes the two in the audit log so retrospective
        readers can tell operator-typed commands from LLM-generated ones.
        """
        t0 = time.monotonic()
        sink: Callable[[str], None] | None = None
        if self._app is not None:
            # Echo: `$ ...` for LLM-routed, `! ...` for direct (already echoed
            # by the TUI's input handler in the direct case; but the agent
            # thread does it for LLM-routed).
            if source == "llm":
                self._app.write_top(f"$ {command}")
            self._app.set_status("running bash")
            sink = lambda line: self._app.write_top(line)

        def _pre_exec() -> None:
            if self._logger:
                self._logger.emit(
                    "bash_pre_exec",
                    command=command,
                    timeout_sec=timeout_sec,
                    source=source,
                )

        def _proc_sink(proc: subprocess.Popen) -> None:
            with self._proc_lock:
                self._active_proc = proc

        # Clear any stale cancel signal from a previous call before starting.
        self._cancel_event.clear()

        try:
            result, meta = tool_bash(
                command,
                timeout_sec=timeout_sec,
                line_sink=sink,
                pre_exec=_pre_exec,
                cwd=self._current_cwd,
                proc_sink=_proc_sink,
                cancel_event=self._cancel_event,
            )
        finally:
            with self._proc_lock:
                self._active_proc = None
            self._cancel_event.clear()

        # Sticky-cwd update: if the command moved us, follow it.
        new_cwd = meta.get("cwd")
        if new_cwd and Path(new_cwd).exists() and new_cwd != self._current_cwd:
            self._current_cwd = new_cwd
            if self._app is not None:
                try:
                    self._app.set_cwd(new_cwd)
                except Exception:  # noqa: BLE001
                    pass

        if self._app is not None:
            try:
                self._app.set_status("idle")
            except Exception:  # noqa: BLE001
                pass

        if self._logger:
            self._logger.emit(
                "tool_call",
                tool="bash",
                args={"command": command, "timeout_sec": timeout_sec},
                result=result,
                duration_ms=meta["duration_ms"],
                exit=meta["exit"],
                timeout=meta["timeout"],
                cancelled=meta.get("cancelled", False),
                source=source,
            )
        return result

    def run_direct(self, command: str, timeout_sec: int = DEFAULT_BASH_TIMEOUT) -> str:
        """Operator-initiated direct exec (`!` prefix). Same as `forward` but
        tagged `source="direct"` in audit so retrospective reads can tell
        operator-typed commands from LLM-generated ones.
        """
        return self._exec(command, timeout_sec, source="direct")


class SearchTool(Tool):
    name = "search"
    description = (
        "Web search. Returns ranked snippets with titles and URLs. Use when you "
        "don't recognize a name or need today's information (package versions, "
        "CVEs, current docs). Don't spam: one search per question is usually "
        "enough; then `visit` the best URL for detail."
    )
    inputs = {
        "query": {"type": "string", "description": "Search query."},
        "max_results": {
            "type": "integer",
            "description": "Max results to return (default 5, max 20).",
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(self, logger=None, app=None, backend: Callable[[str], Any] | None = None):
        super().__init__()
        self._logger = logger
        self._app = app
        self._backend = backend

    def forward(self, query: str, max_results: int = 5) -> str:
        if self._logger:
            self._logger.emit("search_pre_exec", query=query, backend="web_search")
        if self._app is not None:
            try:
                self._app.write_top(f"[search] {query!r}")
                self._app.set_status("searching")
            except Exception:  # noqa: BLE001
                pass
        # Per-call backend if injected; otherwise smolagents WebSearchTool.
        result, meta = tool_search(query, max_results=max_results, backend=self._backend)
        if self._app is not None:
            try:
                self._app.write_top(f"[search] → {meta.get('result_count', 0)} results")
                self._app.set_status("idle")
            except Exception:  # noqa: BLE001
                pass
        if self._logger:
            self._logger.emit(
                "tool_call",
                tool="search",
                args={"query": query, "max_results": max_results},
                result=result,
                result_count=meta.get("result_count", 0),
                duration_ms=meta.get("duration_ms", 0),
                backend=meta.get("backend", "web_search"),
            )
        return result


class VisitTool(Tool):
    name = "visit"
    description = (
        "Fetch a single URL via Jina Reader and return its rendered markdown. "
        "Handles JS-rendered pages server-side. Response is capped at 50 KB by "
        "default; a `[truncated]` marker is appended when hit. Use after `search` "
        "has identified a specific URL worth reading — not speculatively."
    )
    inputs = {
        "url": {"type": "string", "description": "Absolute http(s) URL."},
    }
    output_type = "string"

    def __init__(
        self,
        logger=None,
        app=None,
        *,
        jina_api_key: str = "",
        timeout_sec: int = VISIT_DEFAULT_TIMEOUT_SEC,
        max_bytes: int = VISIT_DEFAULT_MAX_BYTES,
        httpx_client: Any = None,
    ):
        super().__init__()
        self._logger = logger
        self._app = app
        self._jina_api_key = jina_api_key
        self._timeout_sec = timeout_sec
        self._max_bytes = max_bytes
        self._httpx_client = httpx_client

    def forward(self, url: str) -> str:
        if self._logger:
            self._logger.emit("visit_pre_exec", url=url)
        if self._app is not None:
            try:
                self._app.write_top(f"[visit] {url}")
                self._app.set_status("visiting")
            except Exception:  # noqa: BLE001
                pass
        result, meta = tool_visit(
            url,
            jina_api_key=self._jina_api_key,
            timeout_sec=self._timeout_sec,
            max_bytes=self._max_bytes,
            httpx_client=self._httpx_client,
        )
        if self._app is not None:
            try:
                self._app.write_top(
                    f"[visit] ← {meta.get('bytes', 0)} bytes"
                    + (" [truncated]" if meta.get("truncated") else "")
                )
                self._app.set_status("idle")
            except Exception:  # noqa: BLE001
                pass
        if self._logger:
            self._logger.emit(
                "tool_call",
                tool="visit",
                args={"url": url},
                result=result,
                bytes=meta.get("bytes", 0),
                duration_ms=meta.get("duration_ms", 0),
                truncated=meta.get("truncated", False),
                status=meta.get("status", -1),
            )
        return result


class AskTool(Tool):
    """Operator confirmation form.

    Blocks the agent thread until the operator selects an option via the
    textual form (or hits Ctrl-C → returns `"__cancelled__"`). In
    one-shot mode (no TUI), reads from stdin instead.
    """

    name = "ask"
    description = (
        "Ask the operator a yes/no (or multiple-choice) question with structured "
        "options. Blocks the agent until the operator answers. ALWAYS use this "
        "before destructive or shared-state-affecting commands instead of typing "
        '"proceed? [y/N]" as plain text. The first option in `options` is the '
        "default. If the operator cancels (Ctrl-C in the form), this returns "
        "`__cancelled__` — abort the proposed action and explain why."
    )
    inputs = {
        "prompt": {"type": "string", "description": "The question text shown to the operator."},
        "options": {
            "type": "array",
            "description": "Non-empty list of choices, e.g. ['yes', 'no']. First option is the default.",
            "items": {"type": "string"},
        },
    }
    output_type = "string"

    CANCELLED = "__cancelled__"

    def __init__(self, logger=None, app=None, stdin=None, stderr=None):
        super().__init__()
        self._logger = logger
        self._app = app
        self._stdin = stdin
        self._stderr = stderr

    def forward(self, prompt: str, options: list[str]) -> str:
        if not isinstance(options, (list, tuple)) or not options:
            return "[ask error: options must be non-empty]"
        opts = [str(o) for o in options if str(o).strip()]
        if not opts:
            return "[ask error: options must be non-empty]"

        if self._logger:
            self._logger.emit("ask_pre_exec", prompt=prompt, options=opts)

        t0 = time.monotonic()
        chosen: str
        cancelled = False
        if self._app is not None:
            chosen = self._app.show_ask_form(prompt, opts)
            cancelled = (chosen == self.CANCELLED)
        else:
            chosen = self._readline_fallback(prompt, opts)
            cancelled = (chosen == self.CANCELLED)

        if self._logger:
            self._logger.emit(
                "tool_call",
                tool="ask",
                args={"prompt": prompt, "options": opts},
                chosen=chosen,
                duration_ms=int((time.monotonic() - t0) * 1000),
                cancelled=cancelled,
            )
        return chosen

    def _readline_fallback(self, prompt: str, options: list[str]) -> str:
        """No-TUI fallback used by one-shot mode and CI."""
        out = self._stderr if self._stderr is not None else sys.stderr
        inp = self._stdin if self._stdin is not None else sys.stdin
        try:
            out.write(f"\n[ask] {prompt}\n")
            for i, o in enumerate(options, start=1):
                marker = " (default)" if i == 1 else ""
                out.write(f"  {i}. {o}{marker}\n")
            out.write("choose (number or text, empty = default): ")
            out.flush()
        except (OSError, ValueError):
            pass
        try:
            line = inp.readline()
        except (KeyboardInterrupt, EOFError):
            return self.CANCELLED
        if not line:
            return self.CANCELLED
        ans = line.strip()
        if not ans:
            return options[0]
        if ans.isdigit():
            idx = int(ans) - 1
            if 0 <= idx < len(options):
                return options[idx]
            return options[0]
        # Match by prefix.
        for o in options:
            if o.lower() == ans.lower():
                return o
        for o in options:
            if o.lower().startswith(ans.lower()):
                return o
        return options[0]


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
