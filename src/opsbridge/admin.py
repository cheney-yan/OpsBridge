"""opsbridge — admin CLI.

Subcommands: install, config, doctor, enable, disable, uninstall.

All subcommands require root. Exit codes: 0 ok, 1 error, 2 warning.
"""
from __future__ import annotations

import argparse
import getpass
import grp
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import textwrap
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PREFIX = Path("/opt/opsbridge/agent")
VENV = PREFIX / ".venv"
PYTHON_DIR = PREFIX / "python"
ETC_DIR = Path("/etc/opsbridge/agent")
CONFIG_PATH = ETC_DIR / "config.toml"
API_KEY_PATH = ETC_DIR / "api.key"
SUDOERS_PATH = Path("/etc/sudoers.d/opsbridge-agent")
SSHD_SNIPPET_PATH = Path("/etc/ssh/sshd_config.d/50-opsbridge-agent.conf")
SSHD_SNIPPET_DISABLED = Path("/etc/ssh/sshd_config.d/50-opsbridge-agent.conf.disabled")
LOG_DIR = Path("/var/log/opsbridge/agent")
AGENT_HOME = Path("/home/agent")
AGENT_SSH_DIR = AGENT_HOME / ".ssh"
AUTHORIZED_KEYS = AGENT_SSH_DIR / "authorized_keys"
LOCAL_BIN_LINK = Path("/usr/local/bin/opsbridge")
LAUNCHER_PATH = Path("/usr/local/bin/opsbridge-agent")
PI_AGENT_DIR = AGENT_HOME / ".pi" / "agent"
PI_SYSTEM_PROMPT = PI_AGENT_DIR / "SYSTEM.md"
PI_AUTH_JSON = PI_AGENT_DIR / "auth.json"
PI_MODELS_JSON = PI_AGENT_DIR / "models.json"
BOOTSTRAP_META = Path("/etc/opsbridge/bootstrap.toml")

DEPLOY_SHARE_CANDIDATES = [
    Path(__file__).parent.parent.parent / "deploy",  # source layout
    Path("/opt/opsbridge-src/deploy"),
    Path("/usr/local/share/opsbridge"),
]

# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

KNOWN_MODELS: dict[str, dict[str, int]] = {
    "claude-opus-4-7":           {"contextWindow": 200_000, "maxTokens":  32_000},
    "claude-opus-4-5":           {"contextWindow": 200_000, "maxTokens":  32_000},
    "claude-sonnet-4-6":         {"contextWindow": 200_000, "maxTokens":  64_000},
    "claude-sonnet-4-5":         {"contextWindow": 200_000, "maxTokens":  64_000},
    "claude-haiku-4-5-20251001": {"contextWindow": 200_000, "maxTokens":   8_192},
    "claude-haiku-4-5":          {"contextWindow": 200_000, "maxTokens":   8_192},
    "gpt-4o":                    {"contextWindow": 128_000, "maxTokens":  16_384},
    "gpt-4o-mini":               {"contextWindow": 128_000, "maxTokens":  16_384},
    "gpt-4.1":                   {"contextWindow": 1_047_576, "maxTokens": 32_768},
    "gpt-4.1-mini":              {"contextWindow": 1_047_576, "maxTokens": 32_768},
    "gpt-4.1-nano":              {"contextWindow": 1_047_576, "maxTokens": 32_768},
    "o1":                        {"contextWindow": 200_000, "maxTokens": 100_000},
    "o3":                        {"contextWindow": 200_000, "maxTokens": 100_000},
    "o4-mini":                   {"contextWindow": 200_000, "maxTokens": 100_000},
}

ANTHROPIC_MODELS_ORDERED = [
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "claude-opus-4-5",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
]

OPENAI_DEFAULT_MODELS = [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "o1",
    "o3",
    "o4-mini",
]

# ---------------------------------------------------------------------------
# Console color helpers (keep dependency-free)
# ---------------------------------------------------------------------------

def _color(code: int, s: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"\x1b[{code}m{s}\x1b[0m"

def ok(s: str) -> str:    return _color(32, s)
def warn(s: str) -> str:  return _color(33, s)
def err(s: str) -> str:   return _color(31, s)
def bold(s: str) -> str:  return _color(1, s)


def require_root() -> None:
    if os.geteuid() != 0:
        print(err("error: opsbridge requires root (use sudo)"), file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def _user_exists(name: str) -> bool:
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def _group_exists(name: str) -> bool:
    try:
        grp.getgrnam(name)
        return True
    except KeyError:
        return False


def _resolve_src_dir() -> Path:
    if BOOTSTRAP_META.exists():
        try:
            with open(BOOTSTRAP_META, "rb") as fh:
                data = tomllib.load(fh)
            src = Path(data.get("src_dir", ""))
            if src.exists():
                return src
        except (OSError, tomllib.TOMLDecodeError):
            pass
    for cand in DEPLOY_SHARE_CANDIDATES:
        if cand.exists():
            return cand.parent
    return Path("/opt/opsbridge-src")


def _find_snippet(name: str) -> Path | None:
    src = _resolve_src_dir()
    candidates = [src / "deploy" / name] + [c / name for c in DEPLOY_SHARE_CANDIDATES]
    for c in candidates:
        if c.exists():
            return c
    return None


def _prompt(label: str, default: str = "", hidden: bool = False) -> str:
    if hidden:
        return getpass.getpass(f"{label}: ").strip()
    prompt = f"{label} [{default}]: " if default else f"{label}: "
    try:
        return input(prompt).strip() or default
    except EOFError:
        return default


def _stat_mode_owner(path: Path) -> tuple[str, str, str]:
    st = path.stat()
    user = pwd.getpwuid(st.st_uid).pw_name
    group = grp.getgrgid(st.st_gid).gr_name
    mode = oct(stat.S_IMODE(st.st_mode))[2:].rjust(4, "0")
    return user, group, mode


def _ensure_dir(path: Path, owner: str, group: str, mode: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    shutil.chown(path, user=owner, group=group)
    os.chmod(path, mode)


def _write_root(path: Path, content: str, *, owner: str, group: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    shutil.chown(path, user=owner, group=group)
    os.chmod(path, mode)


# ---------------------------------------------------------------------------
# install helpers
# ---------------------------------------------------------------------------

def _create_agent_user() -> bool:
    if _user_exists("agent"):
        print(f"  {ok('ok')} agent user already exists")
        return False
    print("  creating agent user ...")
    _run(["useradd", "--system", "--create-home", "--home-dir", str(AGENT_HOME),
          "--shell", "/bin/bash", "--user-group", "agent"])
    return True


def _ensure_venv(use_system_python: bool, src_dir: Path) -> None:
    """Build / refresh the venv at /opt/opsbridge/agent/.venv via uv (admin CLI only)."""
    uv = shutil.which("uv") or "/usr/local/bin/uv"
    PREFIX.mkdir(parents=True, exist_ok=True)

    if use_system_python:
        python_bin = shutil.which("python3") or "/usr/bin/python3"
    else:
        env = os.environ.copy()
        env["UV_PYTHON_INSTALL_DIR"] = str(PYTHON_DIR)
        subprocess.run([uv, "python", "install", "3.12"], check=True, env=env)
        found = subprocess.run(
            [uv, "python", "find", "3.12"], capture_output=True, text=True, env=env
        )
        python_bin = found.stdout.strip()
    if not VENV.exists():
        subprocess.run([uv, "venv", "--python", python_bin, str(VENV)], check=True)
    subprocess.run(
        [uv, "pip", "install", "--python", str(VENV / "bin" / "python"),
         "--upgrade", str(src_dir)],
        check=True,
    )


def _install_pi() -> None:
    """Install pi.dev coding agent via npm. Skips if already in PATH."""
    if shutil.which("pi"):
        return
    npm = shutil.which("npm")
    if npm is None:
        raise RuntimeError(
            "npm not found — install Node.js first (https://nodejs.org/)"
        )
    result = subprocess.run(
        [npm, "install", "-g", "--ignore-scripts", "@mariozechner/pi-coding-agent"],
        check=False,
    )
    if result.returncode != 0:
        # Retry with --force in case a stale file is blocking the symlink
        subprocess.run(
            [npm, "install", "-g", "--ignore-scripts", "--force",
             "@mariozechner/pi-coding-agent"],
            check=True,
        )


def _ensure_sudoers() -> None:
    content = "agent ALL=(ALL) NOPASSWD:ALL\n"
    _write_root(SUDOERS_PATH, content, owner="root", group="root", mode=0o440)
    if shutil.which("visudo"):
        rc = subprocess.run(["visudo", "-cf", str(SUDOERS_PATH)], capture_output=True)
        if rc.returncode != 0:
            raise RuntimeError(f"visudo refused sudoers file: {rc.stderr.decode()}")


def _ensure_sshd_snippet() -> None:
    snippet = _find_snippet("sshd_config.snippet")
    if snippet is None:
        content = textwrap.dedent("""\
            Match User agent
                PasswordAuthentication no
                ChallengeResponseAuthentication no
                KbdInteractiveAuthentication no
                PermitTTY yes
                X11Forwarding no
                AllowTcpForwarding no
                PermitTunnel no
                ExposeAuthInfo yes
                ForceCommand /usr/local/bin/opsbridge-agent
        """)
    else:
        content = snippet.read_text(encoding="utf-8")
    _write_root(SSHD_SNIPPET_PATH, content, owner="root", group="root", mode=0o644)


def _reload_sshd() -> None:
    sshd = shutil.which("sshd") or "/usr/sbin/sshd"
    rc = subprocess.run([sshd, "-t"], capture_output=True)
    if rc.returncode != 0:
        raise RuntimeError(f"sshd -t failed: {rc.stderr.decode()}")
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "reload", "ssh"], check=False)
        subprocess.run(["systemctl", "reload", "sshd"], check=False)
    elif shutil.which("service"):
        subprocess.run(["service", "ssh", "reload"], check=False)


def _ensure_etc_layout() -> None:
    ETC_DIR.mkdir(parents=True, exist_ok=True)
    shutil.chown(ETC_DIR, user="root", group="agent")
    os.chmod(ETC_DIR, 0o750)


def _ensure_authorized_keys() -> None:
    AGENT_SSH_DIR.mkdir(parents=True, exist_ok=True)
    shutil.chown(AGENT_SSH_DIR, user="agent", group="agent")
    os.chmod(AGENT_SSH_DIR, 0o700)
    if not AUTHORIZED_KEYS.exists():
        AUTHORIZED_KEYS.touch()
    shutil.chown(AUTHORIZED_KEYS, user="agent", group="agent")
    os.chmod(AUTHORIZED_KEYS, 0o600)


# The shell rc launcher: fires when the agent user's shell is invoked interactively
# (covers OrbStack proxy / sudo -u agent -i paths that bypass ForceCommand).
_AGENT_LAUNCHER_HEAD = "# opsbridge: auto-launch the agent on interactive login"
_AGENT_LAUNCHER_TAIL = "# opsbridge: end"
_AGENT_LAUNCHER_BODY = """\
if [[ -t 0 && -t 1 && -z "${OPSBRIDGE_SKIP:-}" ]]; then
    exec /usr/local/bin/opsbridge-agent
fi
"""


def _shell_launcher_block() -> str:
    return f"{_AGENT_LAUNCHER_HEAD}\n{_AGENT_LAUNCHER_BODY}{_AGENT_LAUNCHER_TAIL}\n"


def _ensure_shell_launcher() -> None:
    """Install the interactive-login launcher into the agent user's shell rc files."""
    for filename in (".profile", ".bashrc"):
        target = AGENT_HOME / filename
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        block = _shell_launcher_block()
        if _AGENT_LAUNCHER_HEAD in existing:
            lines = existing.splitlines(keepends=True)
            out: list[str] = []
            in_block = False
            for line in lines:
                if _AGENT_LAUNCHER_HEAD in line:
                    in_block = True
                    out.append(block)
                    continue
                if in_block:
                    if _AGENT_LAUNCHER_TAIL in line:
                        in_block = False
                    continue
                out.append(line)
            new_content = "".join(out)
        else:
            sep = "" if not existing or existing.endswith("\n") else "\n"
            new_content = existing + sep + ("\n" if existing else "") + block
        target.write_text(new_content, encoding="utf-8")
        shutil.chown(target, user="agent", group="agent")
        os.chmod(target, 0o644)


def _ensure_symlink() -> None:
    target = VENV / "bin" / "opsbridge"
    if not target.exists():
        raise RuntimeError(f"venv missing console script: {target}")
    if LOCAL_BIN_LINK.is_symlink() or LOCAL_BIN_LINK.exists():
        try:
            LOCAL_BIN_LINK.unlink()
        except OSError:
            pass
    LOCAL_BIN_LINK.symlink_to(target)


# ---------------------------------------------------------------------------
# pi.dev launcher script (written to /usr/local/bin/opsbridge-agent)
# ---------------------------------------------------------------------------

_DEFAULT_SYSTEM_PROMPT = """\
You are an operations assistant running on a server. The user reached you via SSH.

Rules:
- Before running destructive commands (rm, drop, truncate, kill, overwrite), confirm with the user by asking explicitly.
- This host may have NOPASSWD sudo. Use it carefully.
- Be concise. Prefer direct answers and shell commands over lengthy explanations.
- When a task is ambiguous, ask one clarifying question rather than guessing.
"""


def _launcher_script(provider: str, model: str) -> str:
    return (
        "#!/usr/bin/env bash\n"
        "# Generated by opsbridge — do not edit manually; use: opsbridge config\n"
        "set -euo pipefail\n"
        '[ -t 0 ] || { echo "OpsBridge: requires a terminal (TTY). Connect via SSH." >&2; exit 2; }\n'
        f'exec pi --model "{provider}/{model}"\n'
    )


def _write_launcher_script(cfg: dict) -> None:
    content = _launcher_script(cfg["provider"], cfg["model"])
    _write_root(LAUNCHER_PATH, content, owner="root", group="agent", mode=0o550)


def _write_pi_auth(cfg: dict) -> None:
    """Write ~agent/.pi/agent/auth.json with API key as a shell command."""
    auth = {
        cfg["provider"]: {
            "type": "api_key",
            "key": "!cat /etc/opsbridge/agent/api.key",
        }
    }
    _write_root(PI_AUTH_JSON, json.dumps(auth, indent=2) + "\n",
                owner="agent", group="agent", mode=0o600)


def _write_pi_models(cfg: dict) -> None:
    """Write ~agent/.pi/agent/models.json with model metadata and optional baseUrl."""
    base_url = cfg.get("base_url", "").strip()
    models_list = cfg.get("models", [])

    if not base_url and not models_list:
        if PI_MODELS_JSON.exists():
            PI_MODELS_JSON.unlink()
        return

    api_type = "anthropic-messages" if cfg["provider"] == "anthropic" else "openai-completions"
    provider_entry: dict = {"api": api_type}
    if base_url:
        provider_entry["baseUrl"] = base_url
    if models_list:
        provider_entry["models"] = [
            {"id": m["id"], "contextWindow": m["contextWindow"], "maxTokens": m["maxTokens"]}
            for m in models_list
        ]

    models_json = {"providers": {cfg["provider"]: provider_entry}}
    _write_root(PI_MODELS_JSON, json.dumps(models_json, indent=2) + "\n",
                owner="agent", group="agent", mode=0o600)


def _write_system_prompt(force: bool = False) -> None:
    """Write OpsBridge safety rules to ~agent/.pi/agent/SYSTEM.md (pi.dev's native path)."""
    PI_AGENT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.chown(PI_AGENT_DIR.parent.parent, user="agent", group="agent")  # ~/.pi
    shutil.chown(PI_AGENT_DIR.parent, user="agent", group="agent")          # ~/.pi/agent
    shutil.chown(PI_AGENT_DIR, user="agent", group="agent")
    os.chmod(PI_AGENT_DIR, 0o750)
    if PI_SYSTEM_PROMPT.exists() and not force:
        print(f"  {ok('ok')} keeping existing {PI_SYSTEM_PROMPT}")
        return
    _write_root(PI_SYSTEM_PROMPT, _DEFAULT_SYSTEM_PROMPT, owner="agent", group="agent", mode=0o640)
    print(f"  {ok('ok')} wrote {PI_SYSTEM_PROMPT}")


# ---------------------------------------------------------------------------
# Model discovery and selection
# ---------------------------------------------------------------------------

def _discover_models(provider: str, base_url: str, api_key: str) -> list[str]:
    """Fetch model IDs from the provider API. Falls back to built-in lists."""
    try:
        if provider == "anthropic":
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
            ids = [m["id"] for m in data.get("data", []) if m.get("id")]
            return ids if ids else ANTHROPIC_MODELS_ORDERED[:]
        else:
            base = (base_url or "https://api.openai.com/v1").rstrip("/")
            req = urllib.request.Request(
                f"{base}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
            ids = [m["id"] for m in data.get("data", []) if m.get("id")]
            return sorted(ids) if ids else OPENAI_DEFAULT_MODELS[:]
    except Exception:  # noqa: BLE001
        pass
    return ANTHROPIC_MODELS_ORDERED[:] if provider == "anthropic" else OPENAI_DEFAULT_MODELS[:]


def _parse_model_selection(raw: str, max_idx: int) -> list[int]:
    """Parse 'all', '1,3', '1-3', or '2' into 0-based index list."""
    raw = raw.strip()
    if not raw or raw.lower() == "all":
        return list(range(max_idx))
    seen: set[int] = set()
    result: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if "-" in part:
            lo_s, _, hi_s = part.partition("-")
            try:
                lo_i = int(lo_s.strip()) - 1
                hi_i = int(hi_s.strip()) - 1
                for idx in range(max(0, lo_i), min(max_idx - 1, hi_i) + 1):
                    if idx not in seen:
                        seen.add(idx)
                        result.append(idx)
            except ValueError:
                pass
        else:
            try:
                idx = int(part) - 1
                if 0 <= idx < max_idx and idx not in seen:
                    seen.add(idx)
                    result.append(idx)
            except ValueError:
                pass
    return result


def _lookup_or_prompt_model_meta(model_id: str) -> dict:
    """Return {id, contextWindow, maxTokens} — look up KNOWN_MODELS or prompt."""
    meta = KNOWN_MODELS.get(model_id)
    if meta:
        return {"id": model_id, **meta}
    print(f"  '{model_id}' not in local registry — enter token limits.")
    ctx_raw = _prompt("  Context window (tokens)", default="128000")
    max_raw = _prompt("  Max output tokens", default="4096")
    try:
        ctx = int(ctx_raw.replace(",", "").replace("_", ""))
    except ValueError:
        ctx = 128_000
    try:
        max_tok = int(max_raw.replace(",", "").replace("_", ""))
    except ValueError:
        max_tok = 4_096
    return {"id": model_id, "contextWindow": ctx, "maxTokens": max_tok}


def _prompt_default_model(selected_ids: list[str], existing_default: str) -> str:
    """Prompt user to pick the default model from the selected set."""
    if len(selected_ids) == 1:
        return selected_ids[0]
    print()
    print("  Select the default model (used by the launcher):")
    for i, mid in enumerate(selected_ids, 1):
        marker = " *" if mid == existing_default else ""
        print(f"    {i}. {mid}{marker}")
    default_idx = (
        selected_ids.index(existing_default) + 1
        if existing_default in selected_ids
        else 1
    )
    raw = _prompt("  Default model", default=str(default_idx))
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(selected_ids):
            return selected_ids[idx]
    except ValueError:
        pass
    return selected_ids[0]


# ---------------------------------------------------------------------------
# Model config
# ---------------------------------------------------------------------------

def _prompt_model_config(existing: dict | None = None) -> dict:
    existing = existing or {}
    print()
    print(bold("Configure LLM provider"))

    # --- 1. Provider menu ---
    print("  1. Anthropic  (Claude — https://console.anthropic.com)")
    print("  2. OpenAI     (GPT / o-series — https://platform.openai.com)")
    print("  3. Custom     (any OpenAI-compatible endpoint, e.g. Azure / Bedrock proxy)")
    print()

    existing_provider = existing.get("provider", "")
    existing_base_url = existing.get("base_url", "")
    if existing_provider == "anthropic":
        default_choice = "1"
    elif existing_provider == "openai" and existing_base_url:
        default_choice = "3"
    elif existing_provider == "openai":
        default_choice = "2"
    else:
        default_choice = "1"

    choice = ""
    while choice not in ("1", "2", "3"):
        choice = _prompt("  Choice [1/2/3]", default=default_choice)
        if choice not in ("1", "2", "3"):
            print(err("  enter 1, 2, or 3"))

    if choice == "1":
        provider = "anthropic"
        base_url = ""
    elif choice == "2":
        provider = "openai"
        base_url = ""
    else:
        provider = "openai"
        base_url = _prompt(
            "  Base URL (e.g. https://my.proxy.example/v1)",
            default=existing_base_url,
        )
        while not base_url:
            print(err("  base URL is required for a custom endpoint"))
            base_url = _prompt("  Base URL")

    # --- 2. API key ---
    print()
    api_key = ""
    while not api_key:
        api_key = _prompt("API key (hidden)", hidden=True)

    # --- 3. Discover and select models ---
    print()
    print("  Fetching model list...")
    discovered = _discover_models(provider, base_url, api_key)

    print(f"  Available models ({provider}{' — custom endpoint' if base_url else ''}):")
    for i, mid in enumerate(discovered, 1):
        marker = " ←" if mid == existing.get("model", "") else ""
        print(f"    {i:2d}. {mid}{marker}")
    print()
    print("  Select models to register (numbers, ranges like 1-3, comma list, or 'all').")

    existing_ids = [m["id"] for m in existing.get("models", [])]
    if existing_ids:
        default_sel_parts = [
            str(discovered.index(mid) + 1) for mid in existing_ids if mid in discovered
        ]
        default_sel = ",".join(default_sel_parts) if default_sel_parts else "1"
    else:
        default_sel = "1"

    raw_sel = _prompt("  Select", default=default_sel)
    indices = _parse_model_selection(raw_sel, len(discovered))
    if not indices:
        indices = [0]
    selected_ids = [discovered[i] for i in indices]

    models_meta = [_lookup_or_prompt_model_meta(mid) for mid in selected_ids]
    model = _prompt_default_model(selected_ids, existing.get("model", ""))

    return {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "models": models_meta,
    }


def _prompt_pubkey(existing: bytes = b"") -> str:
    print()
    print(bold("Authorize an SSH pubkey"))
    print("Paste the full pubkey line, then press Enter. Empty line to skip.")
    raw = ""
    try:
        raw = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""
    if not raw:
        return ""
    try:
        res = subprocess.run(
            ["ssh-keygen", "-lf", "-"], input=raw, capture_output=True, text=True, timeout=2,
        )
        if res.returncode != 0:
            print(err(f"  rejected: {res.stderr.strip()}"))
            return ""
    except (OSError, subprocess.SubprocessError) as exc:
        print(warn(f"  warning: could not validate (ssh-keygen unavailable: {exc})"))
    return raw


def _append_authorized_key(pubkey: str) -> None:
    if not pubkey:
        return
    existing = ""
    if AUTHORIZED_KEYS.exists():
        existing = AUTHORIZED_KEYS.read_text(encoding="utf-8")
    if pubkey in existing:
        print(f"  {ok('ok')} pubkey already authorized — skipping")
        return
    suffix = "" if existing.endswith("\n") or not existing else "\n"
    new = existing + suffix + pubkey + "\n"
    AUTHORIZED_KEYS.write_text(new, encoding="utf-8")
    shutil.chown(AUTHORIZED_KEYS, user="agent", group="agent")
    os.chmod(AUTHORIZED_KEYS, 0o600)
    print(f"  {ok('ok')} appended pubkey to {AUTHORIZED_KEYS}")


def _write_config(cfg: dict) -> None:
    lines = [
        "# /etc/opsbridge/agent/config.toml",
        f'provider = "{cfg["provider"]}"',
        f'model    = "{cfg["model"]}"',
    ]
    if cfg.get("base_url"):
        lines.append(f'base_url = "{cfg["base_url"]}"')
    lines.append("")
    for m in cfg.get("models", []):
        lines.append("[[models]]")
        lines.append(f'id            = "{m["id"]}"')
        lines.append(f'contextWindow = {m["contextWindow"]}')
        lines.append(f'maxTokens     = {m["maxTokens"]}')
        lines.append("")
    _write_root(CONFIG_PATH, "\n".join(lines), owner="root", group="agent", mode=0o440)
    _write_root(API_KEY_PATH, cfg["api_key"] + "\n", owner="agent", group="agent", mode=0o400)


def _load_existing_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, "rb") as fh:
            data = tomllib.load(fh)
        models = [
            {
                "id": m["id"],
                "contextWindow": m.get("contextWindow", 0),
                "maxTokens": m.get("maxTokens", 0),
            }
            for m in data.get("models", [])
            if m.get("id")
        ]
        return {
            "provider": data.get("provider", ""),
            "model": data.get("model", ""),
            "base_url": data.get("base_url", ""),
            "models": models,
        }
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _env_or(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _build_cfg_from_env() -> dict | None:
    provider = _env_or("OPSBRIDGE_PROVIDER", "openai").lower()
    if provider not in ("openai", "anthropic"):
        return None
    model = _env_or(
        "OPSBRIDGE_MODEL",
        "gpt-4.1-mini" if provider == "openai" else "claude-sonnet-4-6",
    )
    api_key = _env_or("OPSBRIDGE_API_KEY", "")
    if not api_key:
        return None
    base_url = _env_or("OPSBRIDGE_BASE_URL", "")
    meta = KNOWN_MODELS.get(model)
    if meta:
        models = [{"id": model, **meta}]
    else:
        models = [{"id": model, "contextWindow": 128_000, "maxTokens": 4_096}]
    return {"provider": provider, "model": model, "base_url": base_url, "api_key": api_key, "models": models}


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------

def cmd_install(args: argparse.Namespace) -> int:
    require_root()
    src_dir = _resolve_src_dir()
    print(bold("OpsBridge install"))
    print(f"  source: {src_dir}")

    print("[1/6] System user 'agent'...")
    _create_agent_user()

    print("[2/6] Python venv (opsbridge admin CLI)...")
    _ensure_venv(use_system_python=args.use_system_python, src_dir=src_dir)
    print(f"  {ok('ok')} venv at {VENV}")

    print("[3/6] Pi.dev agent...")
    try:
        _install_pi()
        pi_bin = shutil.which("pi") or "pi"
        print(f"  {ok('ok')} pi installed at {pi_bin}")
    except RuntimeError as exc:
        print(f"  {err('error')}: {exc}")
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"  {err('error')}: npm install failed: {exc}")
        return 1

    print("[4/6] Filesystem layout...")
    _ensure_etc_layout()
    _ensure_authorized_keys()
    _ensure_shell_launcher()
    _ensure_symlink()
    print(f"  {ok('ok')} /etc, /home/agent/.ssh, /home/agent/.profile, {LOCAL_BIN_LINK}")

    config_existing = _load_existing_config()
    have_config = CONFIG_PATH.exists() and API_KEY_PATH.exists()
    do_prompt = args.reconfigure or (not args.skip_model_config and not have_config)

    print("[5/6] Model config...")
    if do_prompt:
        env_cfg = _build_cfg_from_env()
        if env_cfg is not None and not args.interactive:
            cfg = env_cfg
            _write_config(cfg)
            print(f"  {ok('ok')} wrote config from OPSBRIDGE_* env")
        else:
            cfg = _prompt_model_config(existing=config_existing)
            _write_config(cfg)
            print(f"  {ok('ok')} wrote {CONFIG_PATH} and {API_KEY_PATH}")
    else:
        cfg = config_existing
        cfg["api_key"] = API_KEY_PATH.read_text(encoding="utf-8").strip() if API_KEY_PATH.exists() else ""
        print(f"  {ok('ok')} keeping existing config (use --reconfigure to change)")
    _write_launcher_script(cfg)
    _write_pi_auth(cfg)
    _write_pi_models(cfg)
    _write_system_prompt()
    print(f"  {ok('ok')} launcher at {LAUNCHER_PATH}")

    env_pubkey = _env_or("OPSBRIDGE_PUBKEY", "")
    if env_pubkey:
        _append_authorized_key(env_pubkey)
    elif args.interactive:
        pubkey = _prompt_pubkey()
        if pubkey:
            _append_authorized_key(pubkey)
    else:
        print(f"  {ok('ok')} pubkey: add keys manually to {AUTHORIZED_KEYS}")

    print("[6/6] sshd + sudoers...")
    _ensure_sudoers()
    _ensure_sshd_snippet()
    try:
        _reload_sshd()
        print(f"  {ok('ok')} sshd validated and reloaded")
    except RuntimeError as exc:
        print(f"  {err('error')}: {exc}")
        return 1

    print()
    print(ok("Done."))
    print(f"  Add operator pubkeys to {AUTHORIZED_KEYS}")
    return 0


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def cmd_config(args: argparse.Namespace) -> int:
    require_root()
    existing = _load_existing_config()
    cfg = _prompt_model_config(existing=existing)
    _write_config(cfg)
    _write_launcher_script(cfg)
    _write_pi_auth(cfg)
    _write_pi_models(cfg)
    print(ok("config updated — launcher and pi.dev auth regenerated."))
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def _check(label: str, predicate, *, kind: str = "error") -> tuple[int, str]:
    try:
        result = predicate()
    except Exception as exc:  # noqa: BLE001
        result = (False, str(exc))
    ok_flag, detail = result if isinstance(result, tuple) else (bool(result), "")
    if ok_flag:
        return 0, f"  {ok('ok')} {label}" + (f" — {detail}" if detail else "")
    sev = 1 if kind == "error" else 2
    tag = err("error") if kind == "error" else warn("warn")
    return sev, f"  {tag} {label}" + (f" — {detail}" if detail else "")


def cmd_doctor(args: argparse.Namespace) -> int:
    require_root()
    print(bold("opsbridge doctor"))
    rc = 0

    def _path_check(path: Path, owner: str, group: str, mode: int):
        def inner():
            if not path.exists():
                return False, f"missing: {path}"
            u, g, m = _stat_mode_owner(path)
            if u != owner or g != group:
                return False, f"{path}: owner {u}:{g} (want {owner}:{group})"
            if int(m, 8) != mode:
                return False, f"{path}: mode {m} (want {oct(mode)[2:].rjust(4,'0')})"
            return True, f"{path} {owner}:{group} {oct(mode)[2:].rjust(4,'0')}"
        return inner

    def _pi_check():
        pi_path = shutil.which("pi")
        if pi_path is None:
            return False, "pi not found — run: npm install -g --ignore-scripts @mariozechner/pi-coding-agent"
        return True, pi_path

    def _authkeys():
        if not AUTHORIZED_KEYS.exists():
            return False, f"missing: {AUTHORIZED_KEYS}"
        if AUTHORIZED_KEYS.stat().st_size == 0:
            return False, "no operator pubkeys yet"
        return True, ""

    def _sshd_t():
        sshd = shutil.which("sshd") or "/usr/sbin/sshd"
        r = subprocess.run([sshd, "-t"], capture_output=True)
        if r.returncode != 0:
            return False, r.stderr.decode().strip()
        return True, ""

    checks = [
        ("agent user exists", lambda: _user_exists("agent"), "error"),
        ("opsbridge venv", lambda: VENV.exists() and (VENV / "bin" / "opsbridge").exists(), "error"),
        ("pi binary", _pi_check, "error"),
        ("launcher script", _path_check(LAUNCHER_PATH, "root", "agent", 0o550), "error"),
        ("/etc/opsbridge/agent/config.toml", _path_check(CONFIG_PATH, "root", "agent", 0o440), "error"),
        ("/etc/opsbridge/agent/api.key", _path_check(API_KEY_PATH, "agent", "agent", 0o400), "error"),
        ("~agent/.pi/agent/auth.json", _path_check(PI_AUTH_JSON, "agent", "agent", 0o600), "error"),
        ("/etc/sudoers.d/opsbridge-agent", _path_check(SUDOERS_PATH, "root", "root", 0o440), "error"),
        ("/etc/ssh/sshd_config.d/50-opsbridge-agent.conf", _path_check(SSHD_SNIPPET_PATH, "root", "root", 0o644), "error"),
        ("/home/agent/.ssh/", _path_check(AGENT_SSH_DIR, "agent", "agent", 0o700), "error"),
        ("/usr/local/bin/opsbridge symlink", lambda: (LOCAL_BIN_LINK.is_symlink(), str(LOCAL_BIN_LINK.resolve()) if LOCAL_BIN_LINK.exists() else "missing"), "error"),
        ("authorized_keys non-empty", _authkeys, "warning"),
        ("sshd config syntax", _sshd_t, "error"),
    ]

    if args.check_orphans:
        checks.append(("agent-owned process tree", _check_orphans, "warning"))

    for label, fn, kind in checks:
        st, line = _check(label, fn, kind=kind)
        print(line)
        rc = max(rc, st)

    print()
    if rc == 0:
        print(ok("All checks passed."))
    elif rc == 2:
        print(warn("Warnings only — agent will still run."))
    else:
        print(err("Errors found — fix above before relying on the agent."))
    return rc


def _check_orphans():
    if not Path("/proc").exists():
        return True, "/proc not available (not Linux) — skipping orphan check"
    if not _user_exists("agent"):
        return True, "agent user doesn't exist"

    agent_uid = pwd.getpwnam("agent").pw_uid
    self_pid = os.getpid()
    orphans: list[tuple[int, int, str]] = []
    proc_dir = Path("/proc")
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == self_pid:
            continue
        try:
            status_text = (entry / "status").read_text(encoding="utf-8")
        except OSError:
            continue
        uid_line = next((l for l in status_text.splitlines() if l.startswith("Uid:")), "")
        parts = uid_line.split()
        if len(parts) < 2 or int(parts[1]) != agent_uid:
            continue
        ppid_line = next((l for l in status_text.splitlines() if l.startswith("PPid:")), "")
        ppid_parts = ppid_line.split()
        ppid = int(ppid_parts[1]) if len(ppid_parts) >= 2 else 0
        try:
            cmdline = (entry / "cmdline").read_text(encoding="utf-8", errors="replace")
            cmdline = cmdline.replace("\x00", " ").strip() or "(no cmdline)"
        except OSError:
            cmdline = "(unknown)"
        orphans.append((pid, ppid, cmdline))

    if not orphans:
        return True, "no agent-owned processes detected"

    real_orphans = [(p, pp, c) for p, pp, c in orphans if pp == 1]
    other = [(p, pp, c) for p, pp, c in orphans if pp != 1]
    if not real_orphans:
        return True, (
            f"{len(other)} agent-owned process(es), all attached to active sessions (no orphans)"
        )
    lines = [f"  pid {p} (ppid {pp}): {c[:80]}" for p, pp, c in real_orphans]
    detail = f"found {len(real_orphans)} orphan process(es) owned by agent:\n" + "\n".join(lines)
    return False, detail


# ---------------------------------------------------------------------------
# enable / disable
# ---------------------------------------------------------------------------

def _set_agent_shell(shell: str) -> None:
    if _user_exists("agent"):
        subprocess.run(["chsh", "-s", shell, "agent"], check=False, capture_output=True)


def cmd_enable(args: argparse.Namespace) -> int:
    require_root()
    if SSHD_SNIPPET_DISABLED.exists() and not SSHD_SNIPPET_PATH.exists():
        SSHD_SNIPPET_DISABLED.rename(SSHD_SNIPPET_PATH)
    elif not SSHD_SNIPPET_PATH.exists():
        _ensure_sshd_snippet()
    _set_agent_shell("/bin/bash")
    _reload_sshd()
    print(ok("agent SSH login enabled."))
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    require_root()
    if SSHD_SNIPPET_PATH.exists():
        SSHD_SNIPPET_PATH.rename(SSHD_SNIPPET_DISABLED)
    _set_agent_shell("/usr/sbin/nologin")
    _reload_sshd()
    print(ok("agent SSH login disabled (existing sessions unaffected)."))
    return 0


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------

def cmd_uninstall(args: argparse.Namespace) -> int:
    require_root()
    if not args.yes:
        print(warn("This will remove:"))
        print(f"  - user 'agent' and {AGENT_HOME}")
        print(f"  - {PREFIX}")
        print(f"  - {ETC_DIR.parent}")
        print(f"  - {SUDOERS_PATH}")
        print(f"  - {SSHD_SNIPPET_PATH}")
        print(f"  - {LOCAL_BIN_LINK}")
        print(f"  - {LAUNCHER_PATH}")
        print(warn(f"  (logs at {LOG_DIR} are preserved)"))
        confirm = _prompt("Type 'uninstall' to confirm")
        if confirm != "uninstall":
            print("aborted.")
            return 1

    for p in [SUDOERS_PATH, SSHD_SNIPPET_PATH, SSHD_SNIPPET_DISABLED, LOCAL_BIN_LINK, LAUNCHER_PATH]:
        try:
            if p.is_symlink() or p.exists():
                p.unlink()
                print(f"  removed {p}")
        except OSError as exc:
            print(f"  {warn('warn')} could not remove {p}: {exc}")

    for d in [PREFIX, ETC_DIR.parent]:
        if d.exists():
            import shutil as _shutil
            _shutil.rmtree(d, ignore_errors=True)
            print(f"  removed {d}")

    if _user_exists("agent"):
        subprocess.run(["userdel", "-r", "agent"], check=False)
        print("  removed user 'agent'")

    try:
        _reload_sshd()
    except Exception:  # noqa: BLE001
        pass
    print(ok("uninstall complete."))
    return 0


# ---------------------------------------------------------------------------
# argparse driver
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="opsbridge", description="OpsBridge admin CLI")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_install = sub.add_parser("install", help="install or refresh the agent")
    sp_install.add_argument("--reconfigure", action="store_true", help="re-prompt for provider/model/key")
    sp_install.add_argument("--skip-model-config", action="store_true", help="skip model prompts on fresh install")
    sp_install.add_argument("--use-system-python", action="store_true", help="use /usr/bin/python3 instead of uv-managed")
    sp_install.add_argument("--interactive", action="store_true", help="prompt for model/key/pubkey interactively")
    sp_install.set_defaults(func=cmd_install)

    sp_config = sub.add_parser("config", help="re-prompt for model config only")
    sp_config.set_defaults(func=cmd_config)

    sp_doctor = sub.add_parser("doctor", help="verify install integrity")
    sp_doctor.add_argument("--check-orphans", action="store_true", help="list agent-owned processes with PPID=1")
    sp_doctor.set_defaults(func=cmd_doctor)

    sp_enable = sub.add_parser("enable", help="restore the sshd ForceCommand snippet")
    sp_enable.set_defaults(func=cmd_enable)

    sp_disable = sub.add_parser("disable", help="move the sshd snippet aside")
    sp_disable.set_defaults(func=cmd_disable)

    sp_uninstall = sub.add_parser("uninstall", help="remove agent install")
    sp_uninstall.add_argument("--yes", action="store_true", help="skip interactive confirmation")
    sp_uninstall.set_defaults(func=cmd_uninstall)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
