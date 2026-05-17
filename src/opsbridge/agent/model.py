"""LiteLLMModel factory.

Both OpenAI and Anthropic providers are dispatched through smolagents'
LiteLLMModel — no provider abstraction beyond what LiteLLM gives us
(per CLAUDE.md). `base_url` is forwarded as `api_base` so Azure /
Bedrock / vLLM / Ollama / proxies all just work.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from smolagents import LiteLLMModel

CONFIG_PATH = Path("/etc/opsbridge/agent/config.toml")
API_KEY_PATH = Path("/etc/opsbridge/agent/api.key")

# [visit] defaults — see PRD-phase2.md §"Web access".
VISIT_DEFAULT_TIMEOUT_SEC = 15
VISIT_DEFAULT_MAX_BYTES = 50_000


@dataclass
class VisitConfig:
    jina_api_key: str = ""
    timeout_sec: int = VISIT_DEFAULT_TIMEOUT_SEC
    max_bytes: int = VISIT_DEFAULT_MAX_BYTES


@dataclass
class ModelConfig:
    provider: str
    model: str
    base_url: str
    api_key: str
    visit: VisitConfig = field(default_factory=VisitConfig)

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


def _load_visit_block(data: dict) -> VisitConfig:
    block = data.get("visit", {}) or {}
    if not isinstance(block, dict):
        return VisitConfig()
    return VisitConfig(
        jina_api_key=str(block.get("jina_api_key", "") or "").strip(),
        timeout_sec=int(block.get("timeout_sec", VISIT_DEFAULT_TIMEOUT_SEC) or VISIT_DEFAULT_TIMEOUT_SEC),
        max_bytes=int(block.get("max_bytes", VISIT_DEFAULT_MAX_BYTES) or VISIT_DEFAULT_MAX_BYTES),
    )


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

    visit = _load_visit_block(data)

    return ModelConfig(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        visit=visit,
    )


def build_model(cfg: ModelConfig) -> LiteLLMModel:
    """Construct a smolagents LiteLLMModel from a ModelConfig."""
    # Silence LiteLLM's "Give Feedback / Get Help: ..." print on exception paths
    # — it goes to stdout and pollutes the operator's TUI.
    try:
        import litellm  # type: ignore
        litellm.suppress_debug_info = True
    except ImportError:
        pass

    kwargs: dict = {"model_id": cfg.model_id, "api_key": cfg.api_key}
    if cfg.base_url:
        kwargs["api_base"] = cfg.base_url
    return LiteLLMModel(**kwargs)


def discover_models(cfg: ModelConfig, *, timeout_sec: int = 8) -> list[str]:
    """Phase 3 §11: fetch the model id list from the configured endpoint.

    Returns one model id per entry. Empty list on any failure — caller
    falls back to free-text entry.
    """
    if not cfg.base_url:
        # No vendor-native /v1/models we want to depend on. Return a
        # hardcoded short-list for the common provider; operator can still
        # type any model id manually.
        if cfg.provider == "anthropic":
            return ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-7"]
        return []
    try:
        import httpx
        base = cfg.base_url.rstrip("/")
        r = httpx.get(
            f"{base}/models",
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            timeout=timeout_sec,
        )
        r.raise_for_status()
        data = r.json()
        items = data.get("data", data) if isinstance(data, dict) else data
        ids: list[str] = []
        for m in items or []:
            if isinstance(m, dict):
                mid = m.get("id") or m.get("name")
                if mid:
                    ids.append(str(mid))
            elif isinstance(m, str):
                ids.append(m)
        return ids
    except Exception:  # noqa: BLE001 — discovery failures fall back gracefully
        return []


def persist_model_in_config(new_model_id: str, config_path: Path = CONFIG_PATH) -> bool:
    """Rewrite the `model = ...` line in config.toml for `/model save`.

    Preserves the rest of the file (comments, other keys, [visit] block)
    via regex replacement on the raw text. Returns True on success.
    """
    if not config_path.exists():
        return False
    import re
    try:
        text = config_path.read_text(encoding="utf-8")
        new_text, n = re.subn(
            r'^(\s*model\s*=\s*)["\'][^"\']*["\']',
            rf'\1"{new_model_id}"',
            text,
            count=1,
            flags=re.MULTILINE,
        )
        if n == 0:
            return False
        config_path.write_text(new_text, encoding="utf-8")
        return True
    except OSError:
        return False
