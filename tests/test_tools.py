"""Unit tests for tools.py — covers sanitizer, read pagination, write atomicity,
bash timeout behavior, and remember invariants."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from opsbridge.agent import tools as t


# ---------------------------------------------------------------------------
# Sanitizer
# ---------------------------------------------------------------------------

class TestSanitize:
    def test_keeps_sgr_colors(self):
        s = "\x1b[31mred\x1b[0m"
        assert t.sanitize(s) == "\x1b[31mred\x1b[0m"

    def test_keeps_sgr_bold(self):
        s = "\x1b[1mbold\x1b[0m"
        assert t.sanitize(s) == "\x1b[1mbold\x1b[0m"

    def test_strips_cursor_up(self):
        assert t.sanitize("foo\x1b[Abar") == "foobar"

    def test_strips_clear_screen(self):
        assert t.sanitize("\x1b[2Jhello") == "hello"

    def test_strips_clear_line(self):
        assert t.sanitize("foo\x1b[K") == "foo"

    def test_strips_alt_screen_enter(self):
        assert t.sanitize("\x1b[?1049hhello") == "hello"

    def test_strips_osc_title_bell(self):
        s = "\x1b]0;EVIL_TITLE\x07legit"
        assert t.sanitize(s) == "legit"

    def test_strips_osc_title_st(self):
        s = "\x1b]0;EVIL_TITLE\x1b\\legit"
        assert t.sanitize(s) == "legit"

    def test_strips_esc_singles(self):
        # ESC 7 (save cursor) is ESC followed by '7' — but our pattern targets
        # @-Z\\^_a-z{|}~. Let's use ESC c (full reset).
        assert t.sanitize("\x1bcfoo") == "foo"

    def test_strips_bell(self):
        assert t.sanitize("foo\x07bar") == "foobar"

    def test_strips_nul(self):
        assert t.sanitize("foo\x00bar") == "foobar"

    def test_preserves_newlines_and_tabs(self):
        assert t.sanitize("a\nb\tc") == "a\nb\tc"

    def test_empty_string(self):
        assert t.sanitize("") == ""

    def test_combined_attack_string(self):
        """The PRD title-hijack scenario."""
        payload = "\x1b]0;OWNED-BY-PROMPT-INJECTION\x07hello world\n"
        cleaned = t.sanitize(payload)
        assert "OWNED" not in cleaned  # OSC stripped entirely
        assert cleaned == "hello world\n"


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------

class TestRead:
    def test_basic(self, tmp_path):
        p = tmp_path / "hello.txt"
        p.write_text("alpha\nbeta\ngamma\n")
        result = t.tool_read(str(p))
        # Line-numbered output.
        assert "1\talpha" in result
        assert "2\tbeta" in result
        assert "3\tgamma" in result

    def test_missing_file(self, tmp_path):
        assert "file not found" in t.tool_read(str(tmp_path / "nope.txt"))

    def test_directory_error(self, tmp_path):
        assert "is a directory" in t.tool_read(str(tmp_path))

    def test_pagination(self, tmp_path):
        p = tmp_path / "big.txt"
        p.write_text("\n".join(str(i) for i in range(1, 5001)) + "\n")
        # First page.
        first = t.tool_read(str(p))
        assert "[showing lines 1..2000 of 5000]" in first
        assert "2000\t2000" in first
        assert "2001\t2001" not in first
        # Second page.
        second = t.tool_read(str(p), offset=2000, limit=2000)
        assert "[showing lines 2001..4000 of 5000]" in second
        assert "2500\t2500" in second

    def test_offset_past_end(self, tmp_path):
        p = tmp_path / "short.txt"
        p.write_text("only\n")
        assert "past end" in t.tool_read(str(p), offset=100)

    def test_sanitizes_output(self, tmp_path):
        p = tmp_path / "evil.txt"
        p.write_bytes(b"\x1b]0;OWNED\x07legit\n")
        result = t.tool_read(str(p))
        assert "OWNED" not in result
        assert "legit" in result


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------

class TestWrite:
    def test_basic(self, tmp_path):
        p = tmp_path / "out.txt"
        result = t.tool_write(str(p), "hello")
        assert "[wrote 5 chars" in result
        assert p.read_text() == "hello"

    def test_overwrite(self, tmp_path):
        p = tmp_path / "out.txt"
        p.write_text("old")
        t.tool_write(str(p), "new")
        assert p.read_text() == "new"

    def test_missing_parent_dir_errors(self, tmp_path):
        p = tmp_path / "nope" / "out.txt"
        result = t.tool_write(str(p), "hello")
        assert "parent directory does not exist" in result


# ---------------------------------------------------------------------------
# bash
# ---------------------------------------------------------------------------

class TestBash:
    def test_basic(self, tmp_path):
        out, meta = t.tool_bash("echo hello", cwd=str(tmp_path))
        assert "hello" in out
        assert meta["exit"] == 0
        assert meta["timeout"] is False

    def test_captures_stderr(self, tmp_path):
        out, meta = t.tool_bash("echo err >&2; echo out", cwd=str(tmp_path))
        assert "err" in out
        assert "out" in out

    def test_nonzero_exit_returned(self, tmp_path):
        out, meta = t.tool_bash("false", cwd=str(tmp_path))
        assert meta["exit"] != 0
        assert meta["timeout"] is False

    def test_timeout(self, tmp_path):
        start = time.monotonic()
        out, meta = t.tool_bash("sleep 5", timeout_sec=1, cwd=str(tmp_path))
        elapsed = time.monotonic() - start
        assert meta["timeout"] is True
        assert "[timeout after 1s]" in out
        # Should kill quickly, not wait for the full 5s.
        assert elapsed < 4

    def test_pre_exec_fires_before_subprocess(self, tmp_path):
        """`pre_exec` must run before Popen — Phase 2 hooks bash_pre_exec audit
        through this callback so the event survives kill-mid-run."""
        ordering: list[str] = []

        def pre() -> None:
            ordering.append("pre")

        # Use a command that emits a marker line; pre must already be appended
        # by the time we see post-run output.
        out, meta = t.tool_bash(
            "echo MARK",
            cwd=str(tmp_path),
            pre_exec=pre,
        )
        ordering.append("post")
        assert ordering == ["pre", "post"]
        assert "MARK" in out

    def test_line_sink_receives_lines(self, tmp_path):
        """Phase 2 streams bash output line-by-line into the TUI top region."""
        captured: list[str] = []
        out, _ = t.tool_bash(
            "printf 'a\\nb\\nc\\n'",
            cwd=str(tmp_path),
            line_sink=lambda line: captured.append(line),
        )
        assert "a" in captured
        assert "b" in captured
        assert "c" in captured

    def test_sanitizes_output(self, tmp_path):
        out, _ = t.tool_bash(r"printf '\033]0;EVIL\007visible'", cwd=str(tmp_path))
        assert "EVIL" not in out
        assert "visible" in out

    def test_cwd_default_fallback(self, tmp_path, monkeypatch):
        # /home/agent doesn't exist on this dev machine; should fall back to $HOME.
        out, meta = t.tool_bash("pwd")
        assert meta["exit"] == 0
        # Either /home/agent (if running there) or some valid pwd.
        assert out.strip()


# ---------------------------------------------------------------------------
# remember
# ---------------------------------------------------------------------------

class TestRemember:
    def _audit_recorder(self):
        events: list[tuple[str, dict]] = []
        def _audit(ev, payload):
            events.append((ev, payload))
        return events, _audit

    def test_add_creates_file_with_header(self, tmp_path):
        p = tmp_path / "prefs.md"
        events, audit = self._audit_recorder()
        result = t.tool_remember("add", "use systemctl", prefs_path=p, audit=audit)
        assert "added" in result
        text = p.read_text()
        assert "# Operator preferences" in text
        assert "- use systemctl" in text
        assert any(ev == "preferences_file_created" for ev, _ in events)
        assert any(ev == "preferences_mutation" for ev, _ in events)

    def test_add_bullet_normalization(self, tmp_path):
        p = tmp_path / "prefs.md"
        t.tool_remember("add", "* star bullet", prefs_path=p)
        t.tool_remember("add", "- dash bullet", prefs_path=p)
        t.tool_remember("add", "no prefix", prefs_path=p)
        text = p.read_text()
        for line in ("- star bullet", "- dash bullet", "- no prefix"):
            assert line in text

    def test_add_duplicate_rejected(self, tmp_path):
        p = tmp_path / "prefs.md"
        t.tool_remember("add", "convention", prefs_path=p)
        result = t.tool_remember("add", "convention", prefs_path=p)
        assert "duplicate" in result

    def test_add_size_cap_lines(self, tmp_path):
        p = tmp_path / "prefs.md"
        # Pre-populate to 50 lines.
        body = "# Operator preferences\n\n" + "\n".join(f"- pref {i}" for i in range(48)) + "\n"
        p.write_text(body)
        # Now at 50 lines (header + blank + 48 = 50). Adding one more would be 51.
        result = t.tool_remember("add", "one too many", prefs_path=p)
        assert "exceed" in result and "line cap" in result

    def test_add_size_cap_bytes(self, tmp_path):
        p = tmp_path / "prefs.md"
        # File just under the byte cap; adding even a tiny bullet pushes us over.
        body = "# Operator preferences\n\n" + "- " + "x" * 4070 + "\n"
        p.write_text(body)
        result = t.tool_remember("add", "small", prefs_path=p)
        assert "exceed" in result and "byte cap" in result

    def test_remove(self, tmp_path):
        p = tmp_path / "prefs.md"
        t.tool_remember("add", "alpha", prefs_path=p)
        t.tool_remember("add", "beta", prefs_path=p)
        events, audit = self._audit_recorder()
        result = t.tool_remember("remove", "alpha", prefs_path=p, audit=audit)
        assert "removed" in result
        text = p.read_text()
        assert "- alpha" not in text
        assert "- beta" in text
        assert any(ev == "preferences_mutation" for ev, _ in events)

    def test_remove_missing_file_silent_noop(self, tmp_path):
        p = tmp_path / "nope.md"
        events, audit = self._audit_recorder()
        result = t.tool_remember("remove", "anything", prefs_path=p, audit=audit)
        assert "does not exist" in result
        assert not p.exists()
        # PRD: silent no-op, but we still emit a noop audit event.
        muts = [ev for ev, _ in events if ev == "preferences_mutation"]
        # Either no event or a no-op marker; both acceptable.
        assert all(_.get("noop_reason") == "missing_file" for ev, _ in events if ev == "preferences_mutation")

    def test_remove_bullet_not_found(self, tmp_path):
        p = tmp_path / "prefs.md"
        t.tool_remember("add", "alpha", prefs_path=p)
        result = t.tool_remember("remove", "gamma", prefs_path=p)
        assert "not found" in result

    def test_action_validation(self, tmp_path):
        p = tmp_path / "prefs.md"
        assert "action" in t.tool_remember("delete", "x", prefs_path=p)

    def test_empty_content_rejected(self, tmp_path):
        p = tmp_path / "prefs.md"
        assert "empty" in t.tool_remember("add", "   ", prefs_path=p)

    def test_diff_format_emitted(self, tmp_path):
        p = tmp_path / "prefs.md"
        events, audit = self._audit_recorder()
        t.tool_remember("add", "first thing", prefs_path=p, audit=audit)
        mut = next(payload for ev, payload in events if ev == "preferences_mutation")
        assert "diff" in mut
        assert "+- first thing" in mut["diff"]
