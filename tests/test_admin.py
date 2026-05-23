"""Admin CLI tests — argument parsing, doctor helpers, launcher generation."""
from __future__ import annotations

import argparse
import pytest

from opsbridge import admin


def test_argparse_install_flags():
    """Parser accepts all install subcommand flags."""
    cases = [
        ["install"],
        ["install", "--reconfigure"],
        ["install", "--skip-model-config"],
        ["install", "--use-system-python"],
        ["install", "--interactive"],
        ["config"],
        ["doctor"],
        ["doctor", "--check-orphans"],
        ["enable"],
        ["disable"],
        ["uninstall", "--yes"],
    ]
    for argv in cases:
        ap = argparse.ArgumentParser()
        sub = ap.add_subparsers(dest="cmd")
        ap_install = sub.add_parser("install")
        ap_install.add_argument("--reconfigure", action="store_true")
        ap_install.add_argument("--skip-model-config", action="store_true")
        ap_install.add_argument("--use-system-python", action="store_true")
        ap_install.add_argument("--interactive", action="store_true")
        sub.add_parser("config")
        ap_doc = sub.add_parser("doctor")
        ap_doc.add_argument("--check-orphans", action="store_true")
        sub.add_parser("enable")
        sub.add_parser("disable")
        ap_un = sub.add_parser("uninstall")
        ap_un.add_argument("--yes", action="store_true")
        ns = ap.parse_args(argv)
        assert ns.cmd == argv[0]


def test_require_root_blocks_non_root(monkeypatch, capsys):
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    with pytest.raises(SystemExit) as exc:
        admin.require_root()
    assert exc.value.code == 1


def test_main_exits_on_no_subcommand(capsys):
    with pytest.raises(SystemExit):
        admin.main([])


def test_shell_launcher_block_is_tty_guarded():
    """The injected rc block must short-circuit when stdin/stdout aren't TTYs."""
    block = admin._shell_launcher_block()
    assert "-t 0" in block and "-t 1" in block
    assert "OPSBRIDGE_SKIP" in block
    assert "/usr/local/bin/opsbridge-agent" in block


def test_ensure_shell_launcher_creates_files(tmp_path, monkeypatch):
    """First install writes both .profile and .bashrc with the launcher block."""
    home = tmp_path / "home" / "agent"
    home.mkdir(parents=True)
    monkeypatch.setattr(admin, "AGENT_HOME", home)
    monkeypatch.setattr(admin.shutil, "chown", lambda *a, **kw: None)
    admin._ensure_shell_launcher()
    for name in (".profile", ".bashrc"):
        text = (home / name).read_text()
        assert admin._AGENT_LAUNCHER_HEAD in text
        assert admin._AGENT_LAUNCHER_TAIL in text
        assert "/usr/local/bin/opsbridge-agent" in text


def test_ensure_shell_launcher_idempotent(tmp_path, monkeypatch):
    """Re-running replaces the block in place; surrounding content survives."""
    home = tmp_path / "home" / "agent"
    home.mkdir(parents=True)
    monkeypatch.setattr(admin, "AGENT_HOME", home)
    monkeypatch.setattr(admin.shutil, "chown", lambda *a, **kw: None)

    (home / ".bashrc").write_text(
        "export PATH=$PATH:/usr/local/bin\n"
        "# opsbridge: auto-launch the agent on interactive login\n"
        "if [ -t 0 ]; then exec /old/path/agent; fi\n"
        "# opsbridge: end\n"
        "alias ll='ls -la'\n"
    )
    admin._ensure_shell_launcher()
    text = (home / ".bashrc").read_text()
    assert "export PATH=$PATH:/usr/local/bin" in text
    assert "alias ll='ls -la'" in text
    assert "/usr/local/bin/opsbridge-agent" in text
    assert "/old/path/agent" not in text
    assert text.count(admin._AGENT_LAUNCHER_HEAD) == 1


def test_launcher_script_anthropic():
    """Launcher for anthropic provider uses ANTHROPIC_API_KEY and correct model prefix."""
    script = admin._launcher_script("anthropic", "claude-opus-4-7")
    assert "ANTHROPIC_API_KEY" in script
    assert 'exec pi --model "anthropic/claude-opus-4-7"' in script
    assert "OPENAI_API_KEY" not in script


def test_launcher_script_openai():
    """Launcher for openai provider uses OPENAI_API_KEY and correct model prefix."""
    script = admin._launcher_script("openai", "gpt-4o")
    assert "OPENAI_API_KEY" in script
    assert 'exec pi --model "openai/gpt-4o"' in script
    assert "ANTHROPIC_API_KEY" not in script


def test_launcher_script_tty_guard():
    """Launcher must exit 2 when stdin is not a TTY."""
    script = admin._launcher_script("anthropic", "claude-sonnet-4-5")
    assert "[ -t 0 ]" in script
    assert "exit 2" in script


def test_launcher_script_reads_api_key_from_file():
    """API key must come from /etc/opsbridge/agent/api.key at runtime, not hardcoded."""
    script = admin._launcher_script("anthropic", "claude-opus-4-7")
    assert "$(cat /etc/opsbridge/agent/api.key)" in script
