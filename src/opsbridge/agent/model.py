"""LiteLLMModel factory.

Both OpenAI and Anthropic providers are dispatched through smolagents'
LiteLLMModel — no provider abstraction beyond what LiteLLM gives us
(per CLAUDE.md). `base_url` is forwarded as `api_base` so Azure /
Bedrock / vLLM / Ollama / proxies all just work.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from smolagents import LiteLLMModel

CONFIG_PATH = Path("/etc/opsbridge/agent/config.toml")
API_KEY_PATH = Path("/etc/opsbridge/agent/api.key")


@dataclass
class ModelConfig:
    provider: str
    model: str
    base_url: str
    api_key: str

    @property
    def model_id(self) -> str:
        """Provider-prefixed model id LiteLLM expects.

        When `base_url` is set we are talking to an OpenAI-compatible proxy
        (Azure / Bedrock proxy / vLLM / litellm gateway / our test proxy /
        …). The proxy speaks OpenAI's protocol regardless of which vendor
        actually serves the model behind it, so we tag the model_id with
        `openai/` to make LiteLLM route over the OpenAI client. When
        `base_url` is empty we go straight to the vendor's native endpoint
        and use the provider prefix as the operator picked it.
        """
        if "/" in self.model:
            return self.model
        effective_provider = "openai" if self.base_url else self.provider
        return f"{effective_provider}/{self.model}"


def load_config(
    config_path: Path = CONFIG_PATH,
    api_key_path: Path = API_KEY_PATH,
) -> ModelConfig:
    """Read config.toml + api.key from /etc/opsbridge/agent/."""
    with open(config_path, "rb") as fh:
        data = tomllib.load(fh)
    provider = data.get("provider", "").strip().lower()
    if provider not in ("openai", "anthropic"):
        raise ValueError(
            f"config.toml: provider must be 'openai' or 'anthropic', got {provider!r}"
        )
    model = data.get("model", "").strip()
    if not model:
        raise ValueError("config.toml: model is required")
    base_url = data.get("base_url", "").strip()

    api_key = api_key_path.read_text(encoding="utf-8").strip()
    if not api_key:
        raise ValueError(f"{api_key_path}: API key file is empty")

    return ModelConfig(
        provider=provider, model=model, base_url=base_url, api_key=api_key
    )


def build_model(cfg: ModelConfig) -> LiteLLMModel:
    """Construct a smolagents LiteLLMModel from a ModelConfig."""
    kwargs: dict = {"model_id": cfg.model_id, "api_key": cfg.api_key}
    if cfg.base_url:
        kwargs["api_base"] = cfg.base_url
    return LiteLLMModel(**kwargs)
