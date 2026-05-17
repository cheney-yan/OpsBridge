"""Agent entrypoint — invoked by sshd `ForceCommand` per SSH session."""
from __future__ import annotations

import sys

from .core import run_session


def main() -> int:
    return run_session()


if __name__ == "__main__":
    sys.exit(main())
