"""opsbridge — admin CLI.

Subcommands: install, config, doctor, enable, disable,
audit preferences, uninstall.

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
import time
import tomllib
from pathlib import Path

from . import __version__

# ---------------------------------------------------------------------------
# Paths (PRD §10)
# ---------------------------------------------------------------------------

PREFIX = Path("/opt/opsbridge/agent")
VENV = PREFIX / ".venv"
PYTHON_DIR = PREFIX / "python"
ETC_DIR = Path("/etc/opsbridge/agent")
CONFIG_PATH = ETC_DIR / "config.toml"
API_KEY_PATH = ETC_DIR / "api.key"
PREFS_PATH = ETC_DIR / "preferences.md"
SUDOERS_PATH = Path("/etc/sudoers.d/opsbridge-agent")
SSHD_SNIPPET_PATH = Path("/etc/ssh/sshd_config.d/50-opsbridge-agent.conf")
SSHD_SNIPPET_DISABLED = Path("/etc/ssh/sshd_config.d/50-opsbridge-agent.conf.disabled")
LOG_DIR = Path("/var/log/opsbridge/agent")
AGENT_HOME = Path("/home/agent")
AGENT_SSH_DIR = AGENT_HOME / ".ssh"
AUTHORIZED_KEYS = AGENT_SSH_DIR / "authorized_keys"
LOCAL_BIN_LINK = Path("/usr/local/bin/opsbridge")
BOOTSTRAP_META = Path("/etc/opsbridge/bootstrap.toml")

DEPLOY_SHARE_CANDIDATES = [
    Path(__file__).parent.parent.parent / "deploy",  # source layout
    Path("/opt/opsbridge-src/deploy"),
    Path("/usr/local/share/opsbridge"),
]

# Console color helpers (keep dependency-free).
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
    return subprocess.run(
        cmd, check=check, capture_output=capture, text=True
    )


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
    """Locate the source tree (for deploy/*.snippet and pip install)."""
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
# install
# ---------------------------------------------------------------------------

def _create_agent_user() -> bool:
    """Create the `agent` system user + group if missing. Returns True if created."""
    if _user_exists("agent"):
        print(f"  {ok('ok')} agent user already exists")
        return False
    print("  creating agent user ...")
    _run(["useradd", "--system", "--create-home", "--home-dir", str(AGENT_HOME),
          "--shell", "/usr/sbin/nologin", "--user-group", "agent"])
    return True


def _ensure_venv(use_system_python: bool, src_dir: Path) -> None:
    """Build / refresh the venv at /opt/opsbridge/agent/.venv via uv."""
    uv = shutil.which("uv") or "/usr/local/bin/uv"
    PREFIX.mkdir(parents=True, exist_ok=True)

    if use_system_python:
        python_bin = shutil.which("python3") or "/usr/bin/python3"
    else:
        env = os.environ.copy()
        env["UV_PYTHON_INSTALL_DIR"] = str(PYTHON_DIR)
        subprocess.run([uv, "python", "install", "3.12"], check=True, env=env)
        # Locate the installed python.
        found = subprocess.run(
            [uv, "python", "find", "3.12"], capture_output=True, text=True, env=env
        )
        python_bin = found.stdout.strip()
    if not VENV.exists():
        subprocess.run([uv, "venv", "--python", python_bin, str(VENV)], check=True)
    # Install / upgrade the project.
    subprocess.run(
        [uv, "pip", "install", "--python", str(VENV / "bin" / "python"),
         "--upgrade", str(src_dir)],
        check=True,
    )


def _ensure_sudoers() -> None:
    content = "agent ALL=(ALL) NOPASSWD:ALL\n"
    _write_root(SUDOERS_PATH, content, owner="root", group="root", mode=0o440)
    # Validate with visudo if available.
    if shutil.which("visudo"):
        rc = subprocess.run(["visudo", "-cf", str(SUDOERS_PATH)], capture_output=True)
        if rc.returncode != 0:
            raise RuntimeError(f"visudo refused sudoers file: {rc.stderr.decode()}")


def _ensure_sshd_snippet() -> None:
    snippet = _find_snippet("sshd_config.snippet")
    if snippet is None:
        # Fall back to a hardcoded one.
        content = textwrap.dedent("""\
            Match User agent
                PasswordAuthentication no
                ChallengeResponseAuthentication no
                KbdInteractiveAuthentication no
                PermitTTY yes
                X11Forwarding no
                AllowTcpForwarding no
                PermitTunnel no
                ForceCommand /opt/opsbridge/agent/.venv/bin/agent
        """)
    else:
        content = snippet.read_text(encoding="utf-8")
    _write_root(SSHD_SNIPPET_PATH, content, owner="root", group="root", mode=0o644)


def _reload_sshd() -> None:
    # Validate first.
    sshd = shutil.which("sshd") or "/usr/sbin/sshd"
    rc = subprocess.run([sshd, "-t"], capture_output=True)
    if rc.returncode != 0:
        raise RuntimeError(f"sshd -t failed: {rc.stderr.decode()}")
    if shutil.which("systemctl"):
        # `reload` is gentler than `restart` and keeps existing sessions.
        subprocess.run(["systemctl", "reload", "ssh"], check=False)
        subprocess.run(["systemctl", "reload", "sshd"], check=False)
    elif shutil.which("service"):
        subprocess.run(["service", "ssh", "reload"], check=False)


def _ensure_etc_layout() -> None:
    """Create /etc/opsbridge/agent/ with correct ownership and an empty prefs stub."""
    ETC_DIR.mkdir(parents=True, exist_ok=True)
    shutil.chown(ETC_DIR, user="root", group="agent")
    os.chmod(ETC_DIR, 0o750)

    # An empty preferences stub (PRD §6 example header).
    if not PREFS_PATH.exists():
        try:
            import socket
            hostname = socket.gethostname()
        except Exception:
            hostname = "this host"
        stub = f"# Operator preferences for {hostname}\n\n"
        _write_root(PREFS_PATH, stub, owner="root", group="agent", mode=0o640)


def _ensure_log_dir() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    shutil.chown(LOG_DIR, user="root", group="agent")
    os.chmod(LOG_DIR, 0o770)


def _ensure_authorized_keys() -> None:
    AGENT_SSH_DIR.mkdir(parents=True, exist_ok=True)
    shutil.chown(AGENT_SSH_DIR, user="agent", group="agent")
    os.chmod(AGENT_SSH_DIR, 0o700)
    if not AUTHORIZED_KEYS.exists():
        AUTHORIZED_KEYS.touch()
    shutil.chown(AUTHORIZED_KEYS, user="agent", group="agent")
    os.chmod(AUTHORIZED_KEYS, 0o600)


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


def _prompt_model_config(existing: dict | None = None) -> dict:
    existing = existing or {}
    print()
    print(bold("Configure LLM"))
    provider = ""
    while provider not in ("openai", "anthropic"):
        provider = _prompt("Provider [anthropic/openai]",
                           default=existing.get("provider", "anthropic")).lower()
        if provider not in ("openai", "anthropic"):
            print(err("  must be 'openai' or 'anthropic'"))
    default_model = existing.get("model", "gpt-4o" if provider == "openai" else "claude-sonnet-4-5")
    model = _prompt("Model", default=default_model)
    base_url = _prompt("Custom base URL (empty = official)", default=existing.get("base_url", ""))
    api_key = ""
    while not api_key:
        api_key = _prompt("Paste API key (hidden)", hidden=True)
    return {"provider": provider, "model": model, "base_url": base_url, "api_key": api_key}


def _write_config(cfg: dict) -> None:
    body = textwrap.dedent(f"""\
        # /etc/opsbridge/agent/config.toml
        provider = "{cfg['provider']}"
        model    = "{cfg['model']}"
        base_url = "{cfg.get('base_url', '')}"
    """)
    _write_root(CONFIG_PATH, body, owner="root", group="agent", mode=0o440)
    _write_root(API_KEY_PATH, cfg["api_key"] + "\n", owner="agent", group="agent", mode=0o400)


def _load_existing_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, "rb") as fh:
            data = tomllib.load(fh)
        return {
            "provider": data.get("provider", ""),
            "model": data.get("model", ""),
            "base_url": data.get("base_url", ""),
        }
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def cmd_install(args: argparse.Namespace) -> int:
    require_root()
    src_dir = _resolve_src_dir()
    print(bold("OpsBridge install"))
    print(f"  source: {src_dir}")

    print(f"[1/5] System user 'agent'...")
    _create_agent_user()

    print(f"[2/5] Python interpreter + venv...")
    _ensure_venv(use_system_python=args.use_system_python, src_dir=src_dir)
    print(f"  {ok('ok')} venv at {VENV}")

    print(f"[3/5] Filesystem layout...")
    _ensure_etc_layout()
    _ensure_log_dir()
    _ensure_authorized_keys()
    _ensure_symlink()
    print(f"  {ok('ok')} /etc, /var/log, /home/agent/.ssh, /usr/local/bin/opsbridge")

    config_existing = _load_existing_config()
    do_prompt = args.reconfigure or (
        not args.skip_model_config and not (CONFIG_PATH.exists() and API_KEY_PATH.exists())
    )

    print(f"[4/5] LLM config...")
    if do_prompt:
        cfg = _prompt_model_config(existing=config_existing)
        _write_config(cfg)
        print(f"  {ok('ok')} wrote {CONFIG_PATH} and {API_KEY_PATH}")
    else:
        print(f"  {ok('ok')} keeping existing config (use --reconfigure to change)")

    print(f"[5/5] sshd + sudoers...")
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
    print(ok("config updated."))
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def _check(label: str, predicate, *, kind: str = "error") -> tuple[int, str]:
    """Returns (status, message) where status in {0=ok, 1=error, 2=warning}."""
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

    checks = [
        ("agent user exists", lambda: _user_exists("agent"), "error"),
        ("venv present", lambda: VENV.exists() and (VENV / "bin" / "agent").exists(), "error"),
        ("/etc/opsbridge/agent/config.toml", _path_check(CONFIG_PATH, "root", "agent", 0o440), "error"),
        ("/etc/opsbridge/agent/api.key", _path_check(API_KEY_PATH, "agent", "agent", 0o400), "error"),
        ("/etc/opsbridge/agent/preferences.md", _path_check(PREFS_PATH, "root", "agent", 0o640), "warning"),
        ("/etc/sudoers.d/opsbridge-agent", _path_check(SUDOERS_PATH, "root", "root", 0o440), "error"),
        ("/etc/ssh/sshd_config.d/50-opsbridge-agent.conf", _path_check(SSHD_SNIPPET_PATH, "root", "root", 0o644), "error"),
        ("/var/log/opsbridge/agent/", _path_check(LOG_DIR, "root", "agent", 0o770), "error"),
        ("/home/agent/.ssh/", _path_check(AGENT_SSH_DIR, "agent", "agent", 0o700), "error"),
        ("/usr/local/bin/opsbridge symlink", lambda: (LOCAL_BIN_LINK.is_symlink(), str(LOCAL_BIN_LINK.resolve()) if LOCAL_BIN_LINK.exists() else "missing"), "error"),
    ]

    # authorized_keys: warn (not error) if empty.
    def _authkeys():
        if not AUTHORIZED_KEYS.exists():
            return False, f"missing: {AUTHORIZED_KEYS}"
        if AUTHORIZED_KEYS.stat().st_size == 0:
            return False, "no operator pubkeys yet"
        return True, ""
    checks.append(("authorized_keys non-empty", _authkeys, "warning"))

    # sshd -t.
    def _sshd_t():
        sshd = shutil.which("sshd") or "/usr/sbin/sshd"
        r = subprocess.run([sshd, "-t"], capture_output=True)
        if r.returncode != 0:
            return False, r.stderr.decode().strip()
        return True, ""
    checks.append(("sshd config syntax", _sshd_t, "error"))

    for label, fn, kind in checks:
        st, line = _check(label, fn, kind=kind)
        print(line)
        rc = max(rc, st)

    if args.check_api:
        st, line = _check("LLM round-trip", _check_api, kind="error")
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


def _check_api():
    """Tiny LLM round-trip to confirm reachability."""
    try:
        from opsbridge.agent.model import load_config, build_model
        cfg = load_config()
        model = build_model(cfg)
        try:
            resp = model([{"role": "user", "content": "Say the word 'ok' and nothing else."}])
            text = getattr(resp, "content", "") or str(resp)
            return True, f"{cfg.model_id}: {text[:40]}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{cfg.model_id} unreachable: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# ---------------------------------------------------------------------------
# enable / disable
# ---------------------------------------------------------------------------

def cmd_enable(args: argparse.Namespace) -> int:
    require_root()
    if SSHD_SNIPPET_DISABLED.exists() and not SSHD_SNIPPET_PATH.exists():
        SSHD_SNIPPET_DISABLED.rename(SSHD_SNIPPET_PATH)
    elif not SSHD_SNIPPET_PATH.exists():
        _ensure_sshd_snippet()
    _reload_sshd()
    print(ok("agent SSH login enabled."))
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    require_root()
    if SSHD_SNIPPET_PATH.exists():
        SSHD_SNIPPET_PATH.rename(SSHD_SNIPPET_DISABLED)
    _reload_sshd()
    print(ok("agent SSH login disabled (existing sessions unaffected)."))
    return 0


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------

def cmd_audit_preferences(args: argparse.Namespace) -> int:
    require_root()
    canonical: list[dict] = []
    suspicious: list[dict] = []
    if not LOG_DIR.exists():
        print(warn(f"no logs at {LOG_DIR}"))
        return 0
    for path in sorted(LOG_DIR.glob("*.jsonl")):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ev = rec.get("event", "")
                    if ev in ("preferences_mutation", "preferences_file_created"):
                        canonical.append({"file": path.name, **rec})
                    elif ev == "tool_call" and rec.get("tool") in ("bash", "write"):
                        args_blob = rec.get("args", {})
                        args_str = json.dumps(args_blob)
                        if "preferences.md" in args_str or str(PREFS_PATH) in args_str:
                            suspicious.append({"file": path.name, **rec})
        except OSError:
            continue

    print(bold("== canonical (remember events) =="))
    if not canonical:
        print("  (none)")
    for rec in canonical:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec.get("ts", 0)))
        ev = rec.get("event")
        print(f"  [{ts}] {rec['file']} {ev}: {rec.get('action', '')} {rec.get('content', '')}")
        diff = rec.get("diff", "")
        if diff:
            for ln in str(diff).splitlines():
                color = (ok if ln.startswith("+") else err if ln.startswith("-") else lambda s: s)
                print(f"      {color(ln)}")

    print()
    print(bold("== suspicious (bash/write touching preferences.md) =="))
    if not suspicious:
        print("  (none)")
    for rec in suspicious:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec.get("ts", 0)))
        print(f"  [{ts}] {rec['file']} {rec.get('tool')}: {json.dumps(rec.get('args'))[:200]}")
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
        print(warn(f"  (logs at {LOG_DIR} are preserved)"))
        confirm = _prompt("Type 'uninstall' to confirm")
        if confirm != "uninstall":
            print("aborted.")
            return 1

    for p in [SUDOERS_PATH, SSHD_SNIPPET_PATH, SSHD_SNIPPET_DISABLED, LOCAL_BIN_LINK]:
        try:
            if p.is_symlink() or p.exists():
                p.unlink()
                print(f"  removed {p}")
        except OSError as exc:
            print(f"  {warn('warn')} could not remove {p}: {exc}")

    for d in [PREFIX, ETC_DIR.parent]:
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
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
    p = argparse.ArgumentParser(
        prog="opsbridge",
        description="OpsBridge admin CLI",
    )
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_install = sub.add_parser("install", help="install or refresh the agent")
    sp_install.add_argument("--reconfigure", action="store_true", help="re-prompt for provider/model/key")
    sp_install.add_argument("--skip-model-config", action="store_true", help="don't prompt on a fresh install")
    sp_install.add_argument("--use-system-python", action="store_true", help="use /usr/bin/python3 instead of uv-managed")
    sp_install.set_defaults(func=cmd_install)

    sp_config = sub.add_parser("config", help="re-prompt for model config only")
    sp_config.set_defaults(func=cmd_config)

    sp_doctor = sub.add_parser("doctor", help="verify install integrity")
    sp_doctor.add_argument("--check-api", action="store_true", help="also ping the configured LLM")
    sp_doctor.set_defaults(func=cmd_doctor)

    sp_enable = sub.add_parser("enable", help="restore the sshd ForceCommand snippet")
    sp_enable.set_defaults(func=cmd_enable)

    sp_disable = sub.add_parser("disable", help="move the sshd snippet aside")
    sp_disable.set_defaults(func=cmd_disable)

    sp_audit = sub.add_parser("audit", help="audit subcommands")
    audit_sub = sp_audit.add_subparsers(dest="audit_what", required=True)
    sp_audit_prefs = audit_sub.add_parser("preferences", help="show preference mutation history")
    sp_audit_prefs.set_defaults(func=cmd_audit_preferences)

    sp_uninstall = sub.add_parser("uninstall", help="remove agent install")
    sp_uninstall.add_argument("--yes", action="store_true", help="skip interactive confirmation")
    sp_uninstall.set_defaults(func=cmd_uninstall)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
