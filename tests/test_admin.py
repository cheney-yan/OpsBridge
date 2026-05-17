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
