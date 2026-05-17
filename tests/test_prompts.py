"""Tests for prompt_loader — externalized system prompt + override validator."""
from __future__ import annotations

from pathlib import Path

import pytest

from opsbridge.agent import prompt_loader as P


def test_default_prompt_loads_from_package(tmp_path):
    src = P.load_system_prompt(tmp_path / "prefs.md")
    assert src.text.strip()
    for anchor in P.REQUIRED_ANCHORS:
        assert anchor in src.text, f"missing anchor: {anchor!r}"
    assert src.override_used is False
    assert src.rejected is False


def test_validator_anchors_match_default(tmp_path):
    """Guard rail: if the default prompt ever drops an anchor, this fires loud."""
    text, _ = P._load_default_text()
    for anchor in P.REQUIRED_ANCHORS:
        assert anchor in text, f"default prompt missing anchor: {anchor!r}"


def test_override_with_valid_content_used(tmp_path, monkeypatch):
    """An override containing every anchor takes precedence over the default."""
    custom_path = tmp_path / "system_prompt.md"
    custom = "\n".join([
        "Custom prompt for {hostname}.",
        "## Hard rules",
        "- ask before destructive things.",
        "- preferences file is special.",
        "- never fabricate tool output.",
        "- NOPASSWD sudo applies.",
        "",
        "Preferences:",
        "{preferences_block}",
        "Fingerprint: {fingerprint}",
    ])
    custom_path.write_text(custom)
    src = P.load_system_prompt(
        tmp_path / "prefs.md",
        override_path=custom_path,
        hostname="myhost",
        fingerprint="SHA256:test",
    )
    assert src.override_used is True
    assert src.rejected is False
    assert src.path == str(custom_path)
    assert "Custom prompt for myhost." in src.text


def test_override_missing_anchor_rejected(tmp_path):
    """Override missing even one anchor is rejected; default is used instead."""
    broken_path = tmp_path / "system_prompt.md"
    # Drops "ask before destructive".
    broken_path.write_text(
        "Custom\n## Hard rules\npreferences file is special\n"
        "never fabricate tool output\nNOPASSWD sudo\n"
    )
    src = P.load_system_prompt(
        tmp_path / "prefs.md",
        override_path=broken_path,
    )
    assert src.override_used is False
    assert src.rejected is True
    assert "ask before destructive" in src.missing_anchors
    # All anchors still present (we fell back to the default).
    for anchor in P.REQUIRED_ANCHORS:
        assert anchor in src.text


def test_validate_override_file_helper(tmp_path):
    ok_path = tmp_path / "ok.md"
    ok_path.write_text("\n".join(P.REQUIRED_ANCHORS))
    ok_flag, missing = P.validate_override_file(ok_path)
    assert ok_flag is True
    assert missing == ()

    bad_path = tmp_path / "bad.md"
    bad_path.write_text("nothing safety-relevant here")
    ok_flag, missing = P.validate_override_file(bad_path)
    assert ok_flag is False
    assert len(missing) == len(P.REQUIRED_ANCHORS)


def test_prompt_substitution_works(tmp_path):
    src = P.load_system_prompt(
        tmp_path / "prefs.md",
        hostname="alpha-01",
        fingerprint="SHA256:abc",
    )
    assert "alpha-01" in src.text
    assert "SHA256:abc" in src.text
    assert "use `remember`" in src.text  # placeholder when prefs missing
