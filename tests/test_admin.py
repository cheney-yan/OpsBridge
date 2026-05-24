"""Admin CLI tests — argument parsing, doctor helpers, launcher and pi.dev config generation."""
from __future__ import annotations

import argparse
import json
import tomllib
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


# ---------------------------------------------------------------------------
# Model registry constants
# ---------------------------------------------------------------------------

def test_known_models_anthropic():
    m = admin.KNOWN_MODELS["claude-opus-4-7"]
    assert m["contextWindow"] == 200_000
    assert m["maxTokens"] == 32_000


def test_known_models_openai():
    m = admin.KNOWN_MODELS["gpt-4o"]
    assert m["contextWindow"] == 128_000
    assert "maxTokens" in m


def test_anthropic_models_ordered_is_list():
    assert "claude-opus-4-7" in admin.ANTHROPIC_MODELS_ORDERED
    assert "claude-sonnet-4-6" in admin.ANTHROPIC_MODELS_ORDERED


def test_openai_default_models_is_list():
    assert "gpt-4o" in admin.OPENAI_DEFAULT_MODELS
    assert "gpt-4.1-mini" in admin.OPENAI_DEFAULT_MODELS


# ---------------------------------------------------------------------------
# _parse_model_selection
# ---------------------------------------------------------------------------

def test_parse_model_selection_all():
    assert admin._parse_model_selection("all", 4) == [0, 1, 2, 3]


def test_parse_model_selection_empty_string():
    assert admin._parse_model_selection("", 3) == [0, 1, 2]


def test_parse_model_selection_single():
    assert admin._parse_model_selection("2", 5) == [1]


def test_parse_model_selection_comma():
    assert admin._parse_model_selection("1,3", 5) == [0, 2]


def test_parse_model_selection_range():
    assert admin._parse_model_selection("1-3", 5) == [0, 1, 2]


def test_parse_model_selection_out_of_range():
    assert admin._parse_model_selection("99", 3) == []


def test_parse_model_selection_dedup():
    assert admin._parse_model_selection("1,1,2", 5) == [0, 1]


# ---------------------------------------------------------------------------
# _discover_models
# ---------------------------------------------------------------------------

def test_discover_models_anthropic_success(monkeypatch):
    payload = json.dumps({"data": [{"id": "claude-opus-4-7"}, {"id": "claude-sonnet-4-6"}]}).encode()

    class _FakeResp:
        def read(self): return payload
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(admin.urllib.request, "urlopen", lambda *a, **kw: _FakeResp())
    result = admin._discover_models("anthropic", "", "test-key")
    assert result == ["claude-opus-4-7", "claude-sonnet-4-6"]


def test_discover_models_anthropic_fallback(monkeypatch):
    def _raise(*a, **kw):
        raise OSError("network error")
    monkeypatch.setattr(admin.urllib.request, "urlopen", _raise)
    result = admin._discover_models("anthropic", "", "test-key")
    assert result == admin.ANTHROPIC_MODELS_ORDERED


def test_discover_models_openai_fallback(monkeypatch):
    def _raise(*a, **kw):
        raise OSError("network error")
    monkeypatch.setattr(admin.urllib.request, "urlopen", _raise)
    result = admin._discover_models("openai", "", "test-key")
    assert result == admin.OPENAI_DEFAULT_MODELS


# ---------------------------------------------------------------------------
# _lookup_or_prompt_model_meta
# ---------------------------------------------------------------------------

def test_lookup_or_prompt_model_meta_known():
    result = admin._lookup_or_prompt_model_meta("claude-opus-4-7")
    assert result == {"id": "claude-opus-4-7", "contextWindow": 200_000, "maxTokens": 32_000}


def test_lookup_or_prompt_model_meta_unknown():
    result = admin._lookup_or_prompt_model_meta("my-custom-model-v99")
    assert result["id"] == "my-custom-model-v99"
    assert result["contextWindow"] == 128_000
    assert result["maxTokens"] == 4_096


# ---------------------------------------------------------------------------
# _write_config with [[models]]
# ---------------------------------------------------------------------------

def test_write_config_with_models(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(admin, "API_KEY_PATH", tmp_path / "api.key")
    monkeypatch.setattr(admin.shutil, "chown", lambda *a, **kw: None)
    cfg = {
        "provider": "anthropic",
        "model": "claude-opus-4-7",
        "base_url": "",
        "api_key": "test-key",
        "models": [
            {"id": "claude-opus-4-7", "contextWindow": 200_000, "maxTokens": 32_000},
            {"id": "claude-sonnet-4-6", "contextWindow": 200_000, "maxTokens": 64_000},
        ],
    }
    admin._write_config(cfg)
    with open(tmp_path / "config.toml", "rb") as fh:
        data = tomllib.load(fh)
    assert data["provider"] == "anthropic"
    assert data["model"] == "claude-opus-4-7"
    assert len(data["models"]) == 2
    assert data["models"][0]["id"] == "claude-opus-4-7"
    assert data["models"][0]["contextWindow"] == 200_000
    assert data["models"][1]["id"] == "claude-sonnet-4-6"
    assert data["models"][1]["maxTokens"] == 64_000


def test_write_config_no_models(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(admin, "API_KEY_PATH", tmp_path / "api.key")
    monkeypatch.setattr(admin.shutil, "chown", lambda *a, **kw: None)
    cfg = {"provider": "openai", "model": "gpt-4o", "base_url": "", "api_key": "k"}
    admin._write_config(cfg)
    with open(tmp_path / "config.toml", "rb") as fh:
        data = tomllib.load(fh)
    assert data["provider"] == "openai"
    assert "models" not in data


# ---------------------------------------------------------------------------
# _load_existing_config with models
# ---------------------------------------------------------------------------

def test_load_existing_config_with_models(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(
        'provider = "anthropic"\n'
        'model    = "claude-opus-4-7"\n'
        "\n"
        "[[models]]\n"
        'id            = "claude-opus-4-7"\n'
        "contextWindow = 200000\n"
        "maxTokens     = 32000\n"
    )
    monkeypatch.setattr(admin, "CONFIG_PATH", config)
    result = admin._load_existing_config()
    assert result["provider"] == "anthropic"
    assert len(result["models"]) == 1
    assert result["models"][0] == {"id": "claude-opus-4-7", "contextWindow": 200_000, "maxTokens": 32_000}


def test_load_existing_config_no_models_key(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('provider = "openai"\nmodel = "gpt-4o"\n')
    monkeypatch.setattr(admin, "CONFIG_PATH", config)
    result = admin._load_existing_config()
    assert result["models"] == []


# ---------------------------------------------------------------------------
# _write_pi_models with models list
# ---------------------------------------------------------------------------

def test_write_pi_models_with_models_list(tmp_path, monkeypatch):
    models_path = tmp_path / "models.json"
    monkeypatch.setattr(admin, "PI_MODELS_JSON", models_path)
    monkeypatch.setattr(admin.shutil, "chown", lambda *a, **kw: None)
    admin._write_pi_models({
        "provider": "anthropic",
        "base_url": "",
        "models": [{"id": "claude-opus-4-7", "contextWindow": 200_000, "maxTokens": 32_000}],
    })
    data = json.loads(models_path.read_text())
    entry = data["providers"]["anthropic"]
    assert entry["api"] == "anthropic-messages"
    assert "baseUrl" not in entry
    assert entry["models"][0]["id"] == "claude-opus-4-7"
    assert entry["models"][0]["contextWindow"] == 200_000
    assert entry["models"][0]["maxTokens"] == 32_000


def test_write_pi_models_base_url_and_models(tmp_path, monkeypatch):
    models_path = tmp_path / "models.json"
    monkeypatch.setattr(admin, "PI_MODELS_JSON", models_path)
    monkeypatch.setattr(admin.shutil, "chown", lambda *a, **kw: None)
    admin._write_pi_models({
        "provider": "openai",
        "base_url": "https://my.proxy.example/v1",
        "models": [{"id": "gpt-4o", "contextWindow": 128_000, "maxTokens": 16_384}],
    })
    data = json.loads(models_path.read_text())
    entry = data["providers"]["openai"]
    assert entry["baseUrl"] == "https://my.proxy.example/v1"
    assert entry["models"][0]["id"] == "gpt-4o"


# ---------------------------------------------------------------------------
# _build_cfg_from_env with models
# ---------------------------------------------------------------------------

def test_build_cfg_from_env_includes_models(monkeypatch):
    monkeypatch.setenv("OPSBRIDGE_PROVIDER", "anthropic")
    monkeypatch.setenv("OPSBRIDGE_MODEL", "claude-opus-4-7")
    monkeypatch.setenv("OPSBRIDGE_API_KEY", "test-key")
    monkeypatch.delenv("OPSBRIDGE_BASE_URL", raising=False)
    cfg = admin._build_cfg_from_env()
    assert cfg is not None
    assert len(cfg["models"]) == 1
    assert cfg["models"][0]["id"] == "claude-opus-4-7"
    assert cfg["models"][0]["contextWindow"] == 200_000
    assert cfg["models"][0]["maxTokens"] == 32_000


def test_build_cfg_from_env_unknown_model_gets_defaults(monkeypatch):
    monkeypatch.setenv("OPSBRIDGE_PROVIDER", "openai")
    monkeypatch.setenv("OPSBRIDGE_MODEL", "my-custom-proxy-model")
    monkeypatch.setenv("OPSBRIDGE_API_KEY", "test-key")
    monkeypatch.delenv("OPSBRIDGE_BASE_URL", raising=False)
    cfg = admin._build_cfg_from_env()
    assert cfg is not None
    assert cfg["models"][0]["id"] == "my-custom-proxy-model"
    assert cfg["models"][0]["contextWindow"] == 128_000
    assert cfg["models"][0]["maxTokens"] == 4_096
