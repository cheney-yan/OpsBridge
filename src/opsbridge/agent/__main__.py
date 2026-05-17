"""Agent entrypoint — invoked by sshd `ForceCommand` per SSH session."""
from __future__ import annotations

import logging
import os
import sys

from .core import run_session


def _silence_third_party_noise() -> None:
    """Keep LiteLLM / smolagents from streaming retry warnings to the operator's TUI.

    The agent's TUI is stdout. Third-party libs spam stderr with parse-retry
    warnings and Bedrock/SageMaker preload notes that the operator doesn't
    need to see. We redirect this stderr to a process-local logfile under
    /var/log/opsbridge/agent/stderr.log (best-effort; falls back to silent
    discard if we can't write).
    """
    # Send Python logging from these libraries to WARNING+ only, and away from stderr.
    for name in ("LiteLLM", "litellm", "smolagents", "httpx", "anthropic"):
        log = logging.getLogger(name)
        log.setLevel(logging.ERROR)
        log.propagate = False

    # Redirect raw stderr. smolagents prints "Error in code parsing" directly
    # to stderr (not via logging), so we have to swap the fd.
    target = os.environ.get("OPSBRIDGE_STDERR", "/var/log/opsbridge/agent/stderr.log")
    try:
        fh = open(target, "a", buffering=1, encoding="utf-8")
        os.dup2(fh.fileno(), 2)
    except OSError:
        # Fall back to /dev/null if we can't open the target.
        try:
            null = open(os.devnull, "w")
            os.dup2(null.fileno(), 2)
        except OSError:
            pass


def main() -> int:
    _silence_third_party_noise()
    return run_session()


if __name__ == "__main__":
    sys.exit(main())
