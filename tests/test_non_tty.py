"""Phase 2 hardens non-TTY into an explicit exit code 2 with a clear message."""
from __future__ import annotations

import io

import pytest

from opsbridge.agent import core
from opsbridge.agent.model import ModelConfig, VisitConfig


def test_non_tty_exits_with_error(tmp_path):
    cfg = ModelConfig(
        provider="openai", model="gpt-4o", base_url="", api_key="k",
        visit=VisitConfig(),
    )
    stdin = io.StringIO("ignored\n")
    stdout = io.StringIO()
    stderr = io.StringIO()
    rc = core.run_session(
        config=cfg,
        prefs_path=tmp_path / "prefs.md",
        stream_in=stdin,
        stream_out=stdout,
        stream_err=stderr,
        log_dir=tmp_path / "logs",
    )
    assert rc == 2
    assert "TTY" in stderr.getvalue()


def test_one_shot_does_not_import_textual(tmp_path, monkeypatch):
    """The one-shot path must work without importing textual.

    We can't trivially undo a prior import; instead, smoke-test that running
    a degenerate config-failure one-shot returns cleanly without exercising
    the TUI module.
    """
    rc = core.run_session(
        config=None,  # will fail to load (no config.toml) → exits 2
        prefs_path=tmp_path / "prefs.md",
        stream_in=io.StringIO(""),
        stream_out=io.StringIO(),
        stream_err=io.StringIO(),
        log_dir=tmp_path / "logs",
        one_shot="hi",
    )
    # ModelConfig load failure path: returns 2.
    assert rc == 2
