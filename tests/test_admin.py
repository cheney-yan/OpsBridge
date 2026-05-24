"""Admin CLI tests — argument parsing, doctor helpers, launcher and pi.dev config generation."""
from __future__ import annotations

import argparse
import json
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


# ---------------------------------------------------------------------------
# Launcher script
# ---------------------------------------------------------------------------

def test_launcher_script_anthropic():
    script = admin._launcher_script("anthropic", "claude-opus-4-7")
    assert 'exec pi --model "anthropic/claude-opus-4-7"' in script


def test_launcher_script_openai():
    script = admin._launcher_script("openai", "gpt-4o")
    assert 'exec pi --model "openai/gpt-4o"' in script


def test_launcher_script_tty_guard():
    script = admin._launcher_script("anthropic", "claude-sonnet-4-5")
    assert "[ -t 0 ]" in script
    assert "exit 2" in script


def test_launcher_script_no_hardcoded_credentials():
    """Launcher must NOT contain API keys or env var exports — auth.json handles that."""
    script = admin._launcher_script("anthropic", "claude-opus-4-7")
    assert "API_KEY" not in script
    assert "export" not in script
    assert "api.key" not in script


# ---------------------------------------------------------------------------
# Pi.dev auth.json
# ---------------------------------------------------------------------------

def test_write_pi_auth_anthropic(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "PI_AUTH_JSON", tmp_path / "auth.json")
    monkeypatch.setattr(admin.shutil, "chown", lambda *a, **kw: None)
    admin._write_pi_auth({"provider": "anthropic"})
    data = json.loads((tmp_path / "auth.json").read_text())
    assert "anthropic" in data
    assert data["anthropic"]["type"] == "api_key"
    assert data["anthropic"]["key"] == "!cat /etc/opsbridge/agent/api.key"


def test_write_pi_auth_openai(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "PI_AUTH_JSON", tmp_path / "auth.json")
    monkeypatch.setattr(admin.shutil, "chown", lambda *a, **kw: None)
    admin._write_pi_auth({"provider": "openai"})
    data = json.loads((tmp_path / "auth.json").read_text())
    assert "openai" in data
    assert data["openai"]["key"] == "!cat /etc/opsbridge/agent/api.key"


# ---------------------------------------------------------------------------
# Pi.dev models.json (custom base URL)
# ---------------------------------------------------------------------------

def test_write_pi_models_with_base_url(tmp_path, monkeypatch):
    models_path = tmp_path / "models.json"
    monkeypatch.setattr(admin, "PI_MODELS_JSON", models_path)
    monkeypatch.setattr(admin.shutil, "chown", lambda *a, **kw: None)
    admin._write_pi_models({
        "provider": "openai",
        "base_url": "https://my.proxy.example/v1",
    })
    data = json.loads(models_path.read_text())
    assert data["providers"]["openai"]["baseUrl"] == "https://my.proxy.example/v1"
    assert data["providers"]["openai"]["api"] == "openai-completions"


def test_write_pi_models_anthropic_custom(tmp_path, monkeypatch):
    models_path = tmp_path / "models.json"
    monkeypatch.setattr(admin, "PI_MODELS_JSON", models_path)
    monkeypatch.setattr(admin.shutil, "chown", lambda *a, **kw: None)
    admin._write_pi_models({
        "provider": "anthropic",
        "base_url": "https://bedrock.proxy/v1",
    })
    data = json.loads(models_path.read_text())
    assert data["providers"]["anthropic"]["api"] == "anthropic-messages"


def test_write_pi_models_no_base_url_removes_file(tmp_path, monkeypatch):
    """When base_url is empty, models.json should be deleted if it exists."""
    models_path = tmp_path / "models.json"
    models_path.write_text('{"providers":{}}')
    monkeypatch.setattr(admin, "PI_MODELS_JSON", models_path)
    monkeypatch.setattr(admin.shutil, "chown", lambda *a, **kw: None)
    admin._write_pi_models({"provider": "anthropic", "base_url": ""})
    assert not models_path.exists()


def test_write_pi_models_no_base_url_no_file(tmp_path, monkeypatch):
    """When base_url is empty and no file exists, no file should be created."""
    models_path = tmp_path / "models.json"
    monkeypatch.setattr(admin, "PI_MODELS_JSON", models_path)
    monkeypatch.setattr(admin.shutil, "chown", lambda *a, **kw: None)
    admin._write_pi_models({"provider": "anthropic", "base_url": ""})
    assert not models_path.exists()
