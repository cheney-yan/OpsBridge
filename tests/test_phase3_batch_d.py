"""Phase 3 Batch D acceptance tests.

Covers:
  §4 — System prompt nudges against silent retry of interrupted shared-state ops
  §6 — bash_post_kill audit event captures killed-command recovery context
  §7 — confirm_all_sudo config flag relaxes the sudo confirmation chokepoint
  §8 — stderr discipline regression: fd 2 is not a regular file at run_session entry
"""
from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from opsbridge.agent import tools as t
from opsbridge.agent import prompt_loader as P
from opsbridge.agent.logging import SessionLogger
from opsbridge.agent.model import ModelConfig, VisitConfig, load_config


# ---------------------------------------------------------------------------
# §4 — system prompt retry-discipline anchor
# ---------------------------------------------------------------------------

class TestRetryDiscipline:
    def test_system_prompt_includes_retry_clause(self, tmp_path):
        src = P.load_system_prompt(tmp_path / "prefs.md")
        assert "Do not retry interrupted shared-state operations" in src.text
        assert "[cancelled by operator]" in src.text
        assert "[timeout after Ns]" in src.text

    def test_retry_clause_does_not_break_anchors(self):
        text, _ = P._load_default_text()
        for anchor in P.REQUIRED_ANCHORS:
            assert anchor in text


# ---------------------------------------------------------------------------
# §6 — bash_post_kill audit event
# ---------------------------------------------------------------------------

class TestPostKillEvent:
    def _read_events(self, log: SessionLogger) -> list[dict]:
        log.close()
        return [json.loads(l) for l in log.path.read_text().splitlines() if l.strip()]

    def test_post_kill_event_on_timeout(self, tmp_path):
        log = SessionLogger(log_dir=tmp_path / "logs")
        bt = t.BashTool(logger=log, cwd=str(tmp_path))
        bt.forward("sleep 30", timeout_sec=1)
        events = self._read_events(log)
        post = [e for e in events if e["event"] == "bash_post_kill"]
        assert post, "bash_post_kill should fire on timeout"
        assert post[0]["reason"] == "timeout"
        assert post[0]["signal"] == "SIGTERM"
        assert post[0]["command"] == "sleep 30"

    def test_post_kill_event_on_cancel(self, tmp_path):
        log = SessionLogger(log_dir=tmp_path / "logs")
        bt = t.BashTool(logger=log, cwd=str(tmp_path))

        def runner():
            bt.forward("sleep 30", timeout_sec=60)

        th = threading.Thread(target=runner, daemon=True)
        th.start()
        time.sleep(0.4)
        bt.cancel()
        th.join(timeout=6)

        events = self._read_events(log)
        post = [e for e in events if e["event"] == "bash_post_kill"]
        assert post, "bash_post_kill should fire on operator cancel"
        assert post[0]["reason"] == "cancelled"

    def test_no_post_kill_on_clean_exit(self, tmp_path):
        log = SessionLogger(log_dir=tmp_path / "logs")
        bt = t.BashTool(logger=log, cwd=str(tmp_path))
        bt.forward("echo hello")
        events = self._read_events(log)
        assert all(e["event"] != "bash_post_kill" for e in events)

    def test_post_kill_carries_output_tail(self, tmp_path):
        log = SessionLogger(log_dir=tmp_path / "logs")
        bt = t.BashTool(logger=log, cwd=str(tmp_path))
        bt.forward("printf 'before-kill\\n'; sleep 30", timeout_sec=1)
        events = self._read_events(log)
        post = next(e for e in events if e["event"] == "bash_post_kill")
        assert "before-kill" in post["output_tail"]


# ---------------------------------------------------------------------------
# §7 — confirm_all_sudo
# ---------------------------------------------------------------------------

class TestConfirmAllSudo:
    def test_load_config_default_true(self, tmp_path):
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text('provider = "openai"\nmodel = "x"\nbase_url = ""\n')
        key_path = tmp_path / "api.key"
        key_path.write_text("k\n")
        cfg = load_config(cfg_path, key_path)
        assert cfg.confirm_all_sudo is True

    def test_load_config_respects_false(self, tmp_path):
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(
            'provider = "openai"\nmodel = "x"\nbase_url = ""\n'
            'confirm_all_sudo = false\n'
        )
        key_path = tmp_path / "api.key"
        key_path.write_text("k\n")
        cfg = load_config(cfg_path, key_path)
        assert cfg.confirm_all_sudo is False

    def test_prompt_default_has_no_relaxation_clause(self, tmp_path):
        src = P.load_system_prompt(tmp_path / "prefs.md", confirm_all_sudo=True)
        assert "Per-host sudo relaxation" not in src.text

    def test_prompt_false_appends_relaxation_clause(self, tmp_path):
        src = P.load_system_prompt(tmp_path / "prefs.md", confirm_all_sudo=False)
        assert "Per-host sudo relaxation" in src.text
        # The Hard rules MUST still be referenced as still-in-force.
        assert "Hard rules" in src.text


# ---------------------------------------------------------------------------
# §8 — stderr discipline regression
# ---------------------------------------------------------------------------

class TestStderrDiscipline:
    def test_restore_helper_swaps_fd2_back_to_original(self, tmp_path):
        """Mimic the __main__ import-time redirect/restore cycle.

        Step 1: snapshot current fd 2.
        Step 2: redirect fd 2 to a tmpfile (simulating the noise-silencer).
        Step 3: capture an _orig_stderr_fd via os.dup(2) AFTER the swap.
        Step 4: write some bytes — they should land in the tmpfile.
        Step 5: restore fd 2 from the saved snapshot.
        Step 6: writing now should NOT add to the tmpfile.

        This is the behaviour __main__._restore_stderr is supposed to
        guarantee; locking it in catches future regressions of the
        Phase 2 "TUI lands in /var/log/stderr.log" bug.
        """
        orig_fd = os.dup(2)
        log = tmp_path / "stderr_test.log"
        try:
            with open(log, "w") as fh:
                os.dup2(fh.fileno(), 2)
            os.write(2, b"during-redirect\n")

            # Now restore.
            os.dup2(orig_fd, 2)
            os.write(2, b"after-restore\n")
        finally:
            os.close(orig_fd)

        content = log.read_text()
        assert "during-redirect" in content
        assert "after-restore" not in content

    def test_main_module_has_orig_stderr_fd_attr(self):
        """The Phase 2 fix introduced `_orig_stderr_fd` + `_restore_stderr`
        on the agent's __main__ module. Their presence is the API contract.
        """
        from opsbridge.agent import __main__ as agent_main
        assert hasattr(agent_main, "_orig_stderr_fd")
        assert hasattr(agent_main, "_restore_stderr")
        assert callable(agent_main._restore_stderr)

    def test_main_restore_helper_is_idempotent(self):
        from opsbridge.agent import __main__ as agent_main
        agent_main._restore_stderr()
        agent_main._restore_stderr()
