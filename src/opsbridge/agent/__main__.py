"""Agent entrypoint — invoked by sshd `ForceCommand` per SSH session.

stderr-handling discipline (phase 2):
    LiteLLM, botocore, smolagents emit module-load warnings to stderr the
    instant their modules import. The textual TUI writes its full-screen
    rendering through rich's `Console`, which **also** targets stderr by
    default. If we redirect fd 2 to a logfile and leave it that way, the
    LiteLLM noise gets caught — and so does the entire TUI, leaving the
    operator with a blank session.

    The right shape: redirect fd 2 to the logfile **before** importing
    core (so litellm's import-time prints land in the log), then restore
    the original fd 2 before main() runs so textual's rendering reaches
    the SSH client.
"""
from __future__ import annotations

import logging
import os
import sys

# Step 1: capture original stderr fd and redirect to logfile BEFORE any
# third-party import triggers its bedrock/sagemaker preload warnings.
_LOG_TARGET = os.environ.get("OPSBRIDGE_STDERR", "/var/log/opsbridge/agent/stderr.log")
_orig_stderr_fd: int | None = None
try:
    _fh = open(_LOG_TARGET, "a", buffering=1, encoding="utf-8")
    _orig_stderr_fd = os.dup(2)
    os.dup2(_fh.fileno(), 2)
except OSError:
    # Best-effort: fall back to /dev/null so warnings still don't reach the TUI.
    try:
        _null = open(os.devnull, "w")
        _orig_stderr_fd = os.dup(2)
        os.dup2(_null.fileno(), 2)
    except OSError:
        _orig_stderr_fd = None

# Step 2: import the heavy stack (these print to the now-redirected fd 2).
from .core import run_session  # noqa: E402


def _quiet_logging() -> None:
    """Crank third-party Python loggers down to ERROR.

    Tackles runtime warnings (post-import) that the fd 2 swap above only
    catches at import time. Keep this idempotent.
    """
    for name in ("LiteLLM", "litellm", "smolagents", "httpx", "anthropic", "urllib3", "botocore"):
        log = logging.getLogger(name)
        log.setLevel(logging.ERROR)
        log.propagate = False


def _restore_stderr() -> None:
    """Restore the original fd 2 so the textual TUI rendering reaches the SSH client.

    Idempotent: safe to call twice; second call is a no-op.
    """
    global _orig_stderr_fd
    if _orig_stderr_fd is None:
        return
    try:
        os.dup2(_orig_stderr_fd, 2)
        os.close(_orig_stderr_fd)
    except OSError:
        pass
    _orig_stderr_fd = None


def main() -> int:
    _quiet_logging()
    _restore_stderr()
    return run_session()


if __name__ == "__main__":
    sys.exit(main())
