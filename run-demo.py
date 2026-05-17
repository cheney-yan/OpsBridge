#!/usr/bin/env python3
"""Launch an OpsBridge agent against the test LLM proxy from .env.

Run from the repo root in a real terminal (TTY required by the v2 TUI):

    .venv/bin/python run-demo.py

Quit with Ctrl-D. Conversation/preferences persist into /tmp/opsbridge-demo/.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Redirect stderr to a logfile BEFORE anything noisy imports (LiteLLM,
# smolagents, httpx) — otherwise their import-time warnings collide with
# textual's TUI rendering. Then RESTORE stderr immediately before importing
# the agent, because textual's rich.Console writes its full-screen rendering
# through fd 2 by default. If we leave the redirect in place, the entire
# TUI lands in the logfile and the operator's terminal stays blank.
_demo_log_dir = Path("/tmp/opsbridge-demo")
_demo_log_dir.mkdir(parents=True, exist_ok=True)
_stderr_log = _demo_log_dir / "stderr.log"
_orig_stderr_fd = None
try:
    _fh = open(_stderr_log, "a", buffering=1, encoding="utf-8")
    _orig_stderr_fd = os.dup(2)
    os.dup2(_fh.fileno(), 2)
except OSError:
    pass

# Make src/ importable when running from the repo without `pip install -e`.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))

# Load .env (without dotenv to keep this dep-free).
env_path = HERE / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k.strip(), v)

base_url = os.environ.get("AGENT_TEST_LLM_BASE_URL", "")
if not base_url:
    sys.exit(
        "AGENT_TEST_LLM_BASE_URL missing — set it in .env or env. "
        "Use the vendor endpoint (https://api.openai.com/v1, "
        "https://api.anthropic.com/v1) or your own OpenAI-compatible proxy."
    )
if not base_url.endswith("/v1"):
    base_url = base_url.rstrip("/") + "/v1"
api_key = os.environ.get("AGENT_TEST_LLM_KEY", "")
if not api_key:
    sys.exit("AGENT_TEST_LLM_KEY missing — set it in .env or env")

from opsbridge.agent.core import run_session
from opsbridge.agent.model import ModelConfig, VisitConfig

# Restore stderr — textual's rich.Console writes to fd 2 and needs the original
# TTY, not the logfile.
if _orig_stderr_fd is not None:
    try:
        os.dup2(_orig_stderr_fd, 2)
        os.close(_orig_stderr_fd)
    except OSError:
        pass

demo_dir = Path("/tmp/opsbridge-demo")
demo_dir.mkdir(parents=True, exist_ok=True)
prefs_path = demo_dir / "preferences.md"
log_dir = demo_dir / "logs"
log_dir.mkdir(parents=True, exist_ok=True)

cfg = ModelConfig(
    provider="anthropic",
    model="claude-sonnet-4-6",
    base_url=base_url,
    api_key=api_key,
    visit=VisitConfig(),
)

# One-shot mode is opt-in via `--one-shot "..."`.
one_shot = None
if len(sys.argv) >= 3 and sys.argv[1] == "--one-shot":
    one_shot = sys.argv[2]

rc = run_session(
    config=cfg,
    prefs_path=prefs_path,
    log_dir=log_dir,
    one_shot=one_shot,
)
print(f"\n[opsbridge-demo] exit {rc}; logs at {log_dir}, prefs at {prefs_path}")
sys.exit(rc)
