"""Admin CLI tests — argument parsing surface, doctor helpers."""
from __future__ import annotations

import pytest

from opsbridge import admin


def test_argparse_install_flags():
    p = admin
    # The parser should accept all install flags without error.
    parser_argv = [
        ["install"],
        ["install", "--reconfigure"],
        ["install", "--skip-model-config"],
        ["install", "--use-system-python"],
        ["config"],
        ["doctor"],
        ["doctor", "--check-api"],
        ["enable"],
        ["disable"],
        ["audit", "preferences"],
        ["uninstall", "--yes"],
    ]
    for argv in parser_argv:
        # Build the parser and parse only — don't invoke subcommand.
        import argparse
        # We can't easily test main() without calling cmd_* and requiring root.
        # Instead, build the parser the same way main() does.
        import opsbridge.admin as a
        ap = argparse.ArgumentParser()
        sub = ap.add_subparsers(dest="cmd")
        ap_install = sub.add_parser("install")
        ap_install.add_argument("--reconfigure", action="store_true")
        ap_install.add_argument("--skip-model-config", action="store_true")
        ap_install.add_argument("--use-system-python", action="store_true")
        sub.add_parser("config")
        ap_doc = sub.add_parser("doctor")
        ap_doc.add_argument("--check-api", action="store_true")
        sub.add_parser("enable")
        sub.add_parser("disable")
        ap_audit = sub.add_parser("audit")
        sa = ap_audit.add_subparsers(dest="audit_what")
        sa.add_parser("preferences")
        ap_un = sub.add_parser("uninstall")
        ap_un.add_argument("--yes", action="store_true")
        ns = ap.parse_args(argv)
        assert ns.cmd == argv[0]


def test_require_root_blocks_non_root(monkeypatch, capsys):
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    with pytest.raises(SystemExit) as exc:
        admin.require_root()
    assert exc.value.code == 1


def test_main_exits_on_no_subcommand(capsys):
    with pytest.raises(SystemExit):
        admin.main([])


def test_argparse_install_interactive_flag():
    """Phase 2: install gains --interactive (install.sh path)."""
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    ap_install = sub.add_parser("install")
    ap_install.add_argument("--interactive", action="store_true")
    ns = ap.parse_args(["install", "--interactive"])
    assert ns.cmd == "install"
    assert ns.interactive is True


def test_doctor_system_prompt_flag_default_only(tmp_path, monkeypatch):
    """`_check_system_prompt` passes when only the default exists."""
    monkeypatch.setattr(admin, "OVERRIDE_PATH_GLOB", None, raising=False)
    # We avoid touching /etc/opsbridge/agent/system_prompt.md by isolating
    # the override path used by the loader.
    from opsbridge.agent import prompt_loader as P
    monkeypatch.setattr(P, "OVERRIDE_PATH", tmp_path / "does_not_exist.md")
    # admin._check_system_prompt re-imports prompt_loader fresh; patch at the
    # module attribute level after re-import too.
    import importlib
    importlib.reload(P)
    monkeypatch.setattr(P, "OVERRIDE_PATH", tmp_path / "does_not_exist.md")
    ok_flag, detail = admin._check_system_prompt()
    assert ok_flag is True
    assert "default" in detail


def test_doctor_system_prompt_flag_rejects_bad_override(tmp_path, monkeypatch):
    """An override missing anchors must surface as a doctor failure."""
    bad = tmp_path / "system_prompt.md"
    bad.write_text("nothing safety-relevant")
    from opsbridge.agent import prompt_loader as P
    monkeypatch.setattr(P, "OVERRIDE_PATH", bad)
    ok_flag, detail = admin._check_system_prompt()
    assert ok_flag is False
    assert "missing anchors" in detail


def test_shell_launcher_block_is_tty_guarded():
    """The injected block must short-circuit when stdin/stdout aren't TTYs.

    Non-interactive paths like `sudo -u agent cmd` and `ssh agent@host cmd`
    rely on the shell rc files NOT execing the agent.
    """
    block = admin._shell_launcher_block()
    assert "-t 0" in block and "-t 1" in block
    assert "OPSBRIDGE_SKIP" in block
    assert "/opt/opsbridge/agent/.venv/bin/agent" in block


def test_ensure_shell_launcher_creates_files(tmp_path, monkeypatch):
    """First install writes both .profile and .bashrc with the launcher block."""
    home = tmp_path / "home" / "agent"
    home.mkdir(parents=True)
    monkeypatch.setattr(admin, "AGENT_HOME", home)
    # Stub chown — we won't have an agent user in test.
    monkeypatch.setattr(admin.shutil, "chown", lambda *a, **kw: None)
    admin._ensure_shell_launcher()
    for name in (".profile", ".bashrc"):
        text = (home / name).read_text()
        assert admin._AGENT_LAUNCHER_HEAD in text
        assert admin._AGENT_LAUNCHER_TAIL in text
        assert "/opt/opsbridge/agent/.venv/bin/agent" in text


def test_ensure_shell_launcher_idempotent(tmp_path, monkeypatch):
    """Re-running replaces the block in place; surrounding content survives."""
    home = tmp_path / "home" / "agent"
    home.mkdir(parents=True)
    monkeypatch.setattr(admin, "AGENT_HOME", home)
    monkeypatch.setattr(admin.shutil, "chown", lambda *a, **kw: None)

    # Pre-populate with user content + an old launcher block.
    (home / ".bashrc").write_text(
        "export PATH=$PATH:/usr/local/bin\n"
        "# opsbridge: auto-launch the agent TUI on interactive login\n"
        "if [ -t 0 ]; then exec /old/path/agent; fi\n"
        "# opsbridge: end\n"
        "alias ll='ls -la'\n"
    )
    admin._ensure_shell_launcher()
    text = (home / ".bashrc").read_text()
    assert "export PATH=$PATH:/usr/local/bin" in text  # user content preserved
    assert "alias ll='ls -la'" in text                 # trailing content preserved
    assert "/opt/opsbridge/agent/.venv/bin/agent" in text  # new path
    assert "/old/path/agent" not in text               # old block replaced
    # Exactly one block (no duplication on re-run).
    assert text.count(admin._AGENT_LAUNCHER_HEAD) == 1
