#!/usr/bin/env bash
# OpsBridge one-liner installer.
#
#   curl -fsSL <repo>/install.sh | sudo bash
#
# Interactive when /dev/tty is available; env-var driven otherwise:
#   OPSBRIDGE_PROVIDER=openai \
#   OPSBRIDGE_MODEL=gpt-4.1-mini \
#   OPSBRIDGE_API_KEY=... \
#   OPSBRIDGE_PUBKEY="ssh-ed25519 AAAA... me@host" \
#   curl -fsSL .../install.sh | sudo bash
#
# Re-running is safe — choose [k]eep / [r]econfigure / [a]bort on prompt.
set -euo pipefail

# -----------------------------------------------------------------------------
# Pipe → tmpfile self re-exec
# -----------------------------------------------------------------------------
# `curl … | sudo bash` makes bash read its OWN script from stdin (fd 0). Any
# later `read -r` or `exec </dev/tty` to swap fd 0 to the operator's TTY
# breaks bash's ability to keep reading remaining script bytes — under sudo's
# default `use_pty` mode the swap fails with EIO and bash exits with:
#
#     bash: error reading input file: Input/output error
#
# Robust fix: when our $BASH_SOURCE[0] is empty (= we're being read from a
# pipe), slurp the rest of stdin into a tmpfile and `exec bash <tmpfile>`,
# attaching the operator's TTY as the new fd 0. From there on `read` works
# normally and there's no /dev/tty trickery anywhere else in the script.
# Falls back to /dev/null if no controlling terminal (CI / non-interactive
# env-var-driven install).
if [[ -z "${BASH_SOURCE[0]:-}" ]]; then
    _ob_install_tmp=$(mktemp /tmp/opsbridge-install.XXXXXX.sh)
    cat > "$_ob_install_tmp"
    chmod +x "$_ob_install_tmp"
    if (: </dev/tty) 2>/dev/null; then
        exec bash "$_ob_install_tmp" "$@" </dev/tty
    else
        exec bash "$_ob_install_tmp" "$@" </dev/null
    fi
fi

REPO_URL="${OPSBRIDGE_REPO_URL:-https://github.com/cheney-yan/OpsBridge.git}"
REPO_REF="${OPSBRIDGE_REPO_REF:-main}"
SRC_DIR="${OPSBRIDGE_SRC_DIR:-/opt/opsbridge-src}"
SUPPORTS_TTY=0
[[ -t 0 ]] && SUPPORTS_TTY=1

log()   { printf '\033[1;36m[install]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[install]\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[1;31m[install]\033[0m %s\n' "$*" >&2; exit 1; }

if [[ "$(id -u)" -ne 0 ]]; then
    die "must run as root (use sudo). example: curl -fsSL .../install.sh | sudo bash"
fi

# --- 1. platform detection ----------------------------------------------------
KERNEL=$(uname -s)
case "$KERNEL" in
    Linux)  PLATFORM=linux ;;
    Darwin) PLATFORM=macos ;;
    *)      die "OpsBridge supports Linux and macOS. Detected: $KERNEL. File an issue if you need BSD/other support." ;;
esac
log "platform: $PLATFORM"

# --- 2. re-run detection ------------------------------------------------------
ETC=/etc/opsbridge/agent
EXISTING_INSTALL=0
if [[ -f "$ETC/config.toml" ]]; then
    EXISTING_INSTALL=1
fi

CHOICE=""
if [[ "$EXISTING_INSTALL" -eq 1 ]]; then
    log "existing install detected at $ETC/config.toml"
    if [[ "$SUPPORTS_TTY" -eq 1 ]]; then
        printf '  [k]eep config / [r]econfigure / [a]bort? '
        # /dev/tty may still be unreadable even with stdin TTY in some
        # nested sudo/container shells — fall back to keep on read failure.
        if ! read -r CHOICE 2>/dev/null; then
            CHOICE="k"
        fi
        CHOICE=${CHOICE:-k}
    else
        CHOICE="k"
    fi
    case "$CHOICE" in
        k|K) log "keeping existing config — will only refresh venv + sshd snippet" ;;
        r|R) log "will re-prompt for provider/model/api key" ;;
        a|A) die "aborted" ;;
        *)   log "unknown choice $CHOICE — defaulting to [k]" ; CHOICE="k" ;;
    esac
fi

# --- 3. acquire source --------------------------------------------------------
log "acquiring source → $SRC_DIR"
if [[ -d "$SRC_DIR/.git" ]]; then
    git -C "$SRC_DIR" fetch --depth 1 origin "$REPO_REF" || warn "git fetch failed; using cached copy"
    git -C "$SRC_DIR" checkout FETCH_HEAD || git -C "$SRC_DIR" checkout "$REPO_REF" || true
elif command -v git >/dev/null 2>&1; then
    rm -rf "$SRC_DIR"
    # `--branch` only accepts branch/tag names, not commit SHAs. Try the
    # shallow path first; if REPO_REF is a SHA, fall back to a full clone +
    # checkout.
    if ! git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$SRC_DIR" 2>/dev/null; then
        log "  (REPO_REF=$REPO_REF not a branch/tag, falling back to full clone + checkout)"
        rm -rf "$SRC_DIR"
        git clone "$REPO_URL" "$SRC_DIR"
        git -C "$SRC_DIR" checkout "$REPO_REF"
    fi
else
    warn "git missing; trying tarball"
    TARBALL_URL="${REPO_URL%.git}/archive/${REPO_REF}.tar.gz"
    tmp=$(mktemp -d)
    curl -fsSL "$TARBALL_URL" | tar -xz -C "$tmp"
    rm -rf "$SRC_DIR"
    mv "$tmp"/*/  "$SRC_DIR"
fi

# --- 4. bootstrap.sh — venv + admin CLI ---------------------------------------
log "bootstrap (uv + venv) ..."
"$SRC_DIR/bootstrap.sh"

# --- 5. opsbridge install -----------------------------------------------------
ARGS=()
if [[ "$EXISTING_INSTALL" -eq 1 && "$CHOICE" =~ ^[kK]$ ]]; then
    ARGS+=("--skip-model-config")
elif [[ "$EXISTING_INSTALL" -eq 1 && "$CHOICE" =~ ^[rR]$ ]]; then
    ARGS+=("--reconfigure")
fi

# Pick interactive vs env-var driven path.
if [[ -n "${OPSBRIDGE_API_KEY:-}" ]]; then
    log "OPSBRIDGE_API_KEY present — non-interactive env-var install"
elif [[ "$SUPPORTS_TTY" -eq 1 ]]; then
    ARGS+=("--interactive")
else
    die "no TTY and no OPSBRIDGE_API_KEY — set env vars (see install.sh header) or run with a TTY"
fi

log "running: opsbridge install ${ARGS[*]:-(no args)}"
/usr/local/bin/opsbridge install "${ARGS[@]}"

log "done."
log "next: ssh agent@\$(hostname)"
