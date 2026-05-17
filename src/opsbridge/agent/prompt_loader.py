"""System prompt loader — externalized to prompts/system.md.

Loads the default from package data via `importlib.resources`. If
`/etc/opsbridge/agent/system_prompt.md` exists, validates it against
required anchors; uses it iff all anchors are present, otherwise
falls back to the default and records an audit event.
"""
from __future__ import annotations

import hashlib
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

OVERRIDE_PATH = Path("/etc/opsbridge/agent/system_prompt.md")

# Content-level anchors the override must contain verbatim, otherwise
# the override is rejected. These match load-bearing safety vocabulary
# in the default prompt — see PRD-phase2.md §"System prompt
# externalization".
REQUIRED_ANCHORS: tuple[str, ...] = (
    "## Hard rules",
    "ask before destructive",
    "preferences file is special",
    "never fabricate tool output",
    "NOPASSWD sudo",
)


@dataclass
class PromptSource:
    text: str            # post-substitution prompt
    path: str            # default package path or override path
    sha256: str
    override_used: bool
    rejected: bool = False
    missing_anchors: tuple[str, ...] = ()


def _load_default_text() -> tuple[str, str]:
    """Load the bundled default markdown from package data.

    Returns (text, path-string).
    """
    try:
        from importlib.resources import files  # py>=3.9
        ref = files("opsbridge.agent.prompts") / "system.md"
        return ref.read_text(encoding="utf-8"), str(ref)
    except (ModuleNotFoundError, FileNotFoundError):
        # Source-tree fallback for tests / dev.
        here = Path(__file__).parent / "prompts" / "system.md"
        return here.read_text(encoding="utf-8"), str(here)


def _validate_override(text: str) -> tuple[bool, tuple[str, ...]]:
    missing = tuple(a for a in REQUIRED_ANCHORS if a not in text)
    return (len(missing) == 0), missing


def _format_preferences_block(prefs_path: Path) -> str:
    if not prefs_path.exists():
        return "(none yet — use `remember` to record host conventions.)"
    text = prefs_path.read_text(encoding="utf-8").strip()
    if not text:
        return "(none yet — use `remember` to record host conventions.)"
    return text


def load_system_prompt(
    prefs_path: Path,
    *,
    hostname: str | None = None,
    fingerprint: str = "unknown",
    override_path: Path = OVERRIDE_PATH,
) -> PromptSource:
    """Build the system prompt for this session.

    Resolution order:
    1. Load default from package data.
    2. If override_path exists and validates → use it.
    3. Otherwise fall back to default; record `rejected=True` if the
       override existed but failed validation.
    4. Substitute {hostname}/{fingerprint}/{preferences_block}.
    """
    default_text, default_path = _load_default_text()

    used_text = default_text
    used_path = default_path
    override_used = False
    rejected = False
    missing: tuple[str, ...] = ()

    if override_path.exists():
        try:
            otext = override_path.read_text(encoding="utf-8")
            ok, missing = _validate_override(otext)
            if ok:
                used_text = otext
                used_path = str(override_path)
                override_used = True
            else:
                rejected = True
        except OSError:
            rejected = True
            missing = ("(unreadable override)",)

    substituted = used_text.format(
        hostname=hostname or socket.gethostname(),
        fingerprint=fingerprint,
        preferences_block=_format_preferences_block(prefs_path),
    )
    digest = hashlib.sha256(substituted.encode("utf-8")).hexdigest()

    return PromptSource(
        text=substituted,
        path=used_path,
        sha256=digest,
        override_used=override_used,
        rejected=rejected,
        missing_anchors=missing,
    )


def validate_override_file(path: Path) -> tuple[bool, tuple[str, ...]]:
    """Public helper for `opsbridge doctor --system-prompt`."""
    if not path.exists():
        return False, ("(override file does not exist)",)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover — surface as a failure
        return False, (f"(read error: {exc})",)
    return _validate_override(text)
