"""Config loader tests."""
from __future__ import annotations

import pytest

from opsbridge.agent import model


def test_load_config(tmp_path):
    cfg_path = tmp_path / "config.toml"
    key_path = tmp_path / "api.key"
    cfg_path.write_text('provider = "anthropic"\nmodel = "claude-sonnet-4-5"\nbase_url = ""\n')
    key_path.write_text("sk-test-key\n")
    cfg = model.load_config(cfg_path, key_path)
    assert cfg.provider == "anthropic"
    assert cfg.model == "claude-sonnet-4-5"
    assert cfg.api_key == "sk-test-key"
    assert cfg.model_id == "anthropic/claude-sonnet-4-5"


def test_load_config_base_url(tmp_path):
    cfg_path = tmp_path / "config.toml"
    key_path = tmp_path / "api.key"
    cfg_path.write_text('provider = "openai"\nmodel = "gpt-4o-mini"\nbase_url = "https://proxy.example/v1"\n')
    key_path.write_text("k\n")
    cfg = model.load_config(cfg_path, key_path)
    assert cfg.base_url == "https://proxy.example/v1"
    assert cfg.model_id == "openai/gpt-4o-mini"


def test_load_config_invalid_provider(tmp_path):
    cfg_path = tmp_path / "config.toml"
    key_path = tmp_path / "api.key"
    cfg_path.write_text('provider = "bogus"\nmodel = "m"\nbase_url = ""\n')
    key_path.write_text("k\n")
    with pytest.raises(ValueError):
        model.load_config(cfg_path, key_path)


def test_load_config_empty_key(tmp_path):
    cfg_path = tmp_path / "config.toml"
    key_path = tmp_path / "api.key"
    cfg_path.write_text('provider = "openai"\nmodel = "gpt-4o"\nbase_url = ""\n')
    key_path.write_text("\n")
    with pytest.raises(ValueError):
        model.load_config(cfg_path, key_path)


def test_load_config_already_prefixed_model(tmp_path):
    cfg_path = tmp_path / "config.toml"
    key_path = tmp_path / "api.key"
    cfg_path.write_text('provider = "openai"\nmodel = "openai/gpt-4o"\nbase_url = ""\n')
    key_path.write_text("k\n")
    cfg = model.load_config(cfg_path, key_path)
    assert cfg.model_id == "openai/gpt-4o"
