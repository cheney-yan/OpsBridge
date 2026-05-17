"""Light smoke tests for core.py (without running the full agent loop)."""
from __future__ import annotations

from pathlib import Path

from opsbridge.agent import core


def test_format_preferences_block_missing(tmp_path):
    p = tmp_path / "nope.md"
    out = core._format_preferences_block(p)
    assert "use `remember`" in out


def test_format_preferences_block_present(tmp_path):
    p = tmp_path / "prefs.md"
    p.write_text("# header\n\n- use systemctl\n")
    out = core._format_preferences_block(p)
    assert "- use systemctl" in out


def test_system_prompt_contains_safety_rules(tmp_path):
    prompt = core.build_system_prompt(tmp_path / "prefs.md")
    # Spot-check the load-bearing rules from CLAUDE.md.
    assert "NOPASSWD sudo" in prompt
    assert "confirmation" in prompt.lower() or "yes" in prompt.lower()
    assert "preferences" in prompt.lower()
    assert "remember" in prompt.lower()
    assert "fabricate" in prompt.lower() or "honestly" in prompt.lower()


def test_token_budget_known_model():
    b = core.TokenBudget("anthropic/claude-sonnet-4-5")
    assert b.window == 200_000
    b.add(1000)
    assert b.used == 1000
    assert b.ratio == 1000 / 200_000


def test_token_budget_unknown_model_default():
    b = core.TokenBudget("custom/whatever")
    assert b.window == core.TokenBudget.DEFAULT_WINDOW


def test_token_budget_bands():
    b = core.TokenBudget("openai/gpt-4o")
    b.add(int(b.window * 0.85))
    assert core.SOFT_THRESHOLD <= b.ratio < core.COMPRESS_THRESHOLD
