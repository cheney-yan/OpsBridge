#!/usr/bin/env bash
# OpsBridge one-liner installer.
#
# Recommended (interactive, default main):
#     curl -fsSL .../install.sh | bash         ← no leading sudo
#
# Pin a specific commit / branch / tag (REF):
#     curl -fsSL .../REF/install.sh | bash -s REF
#
# Non-interactive (CI / Ansible):
#     OPSBRIDGE_PROVIDER=anthropic \
#     OPSBRIDGE_MODEL=claude-opus-4-7 \
#     OPSBRIDGE_API_KEY=... \
#     OPSBRIDGE_PUBKEY="ssh-ed25519 AAAA... me@host" \
#     curl -fsSL .../install.sh | bash
#
# Why no leading sudo: `curl | sudo bash` is fundamentally broken for
# interactive prompts. This script handles sudo internally so its sudo
# invocation can attach to your REAL TTY.
#
# Re-running is safe — choose [k]eep / [r]econfigure / [a]bort on prompt.
#
# Uninstall:
#     curl -fsSL .../install.sh | bash -s uninstall
set -euo pipefail

# ---------------------------------------------------------------------------
# Step 0: get the script onto disk + re-exec as root with a real TTY.
# ---------------------------------------------------------------------------
_OB_ENV_KEEP=OPSBRIDGE_PROVIDER,OPSBRIDGE_MODEL,OPSBRIDGE_BASE_URL,OPSBRIDGE_API_KEY,OPSBRIDGE_PUBKEY,OPSBRIDGE_REPO_URL,OPSBRIDGE_REPO_REF,OPSBRIDGE_SRC_DIR,OPSBRIDGE_USE_SYSTEM_PYTHON,OPSBRIDGE_SKIP_LLM_CHECK

if [[ -z "${BASH_SOURCE[0]:-}" ]]; then
    _tmp=$(mktemp /tmp/opsbridge-install.XXXXXX.sh)
    cat > "$_tmp"
    chmod +x "$_tmp"
    if [[ "$(id -u)" -eq 0 ]]; then
        if (: </dev/tty) 2>/dev/null; then
            exec bash "$_tmp" "$@" </dev/tty
        else
            exec bash "$_tmp" "$@" </dev/null
        fi
    else
        if (: </dev/tty) 2>/dev/null; then
            exec sudo --preserve-env="$_OB_ENV_KEEP" bash "$_tmp" "$@" </dev/tty
        else
            exec sudo --preserve-env="$_OB_ENV_KEEP" bash "$_tmp" "$@" </dev/null
        fi
    fi
fi

if [[ "$(id -u)" -ne 0 ]]; then
    if (: </dev/tty) 2>/dev/null; then
        exec sudo --preserve-env="$_OB_ENV_KEEP" bash "$0" "$@" </dev/tty
    else
        exec sudo --preserve-env="$_OB_ENV_KEEP" bash "$0" "$@" </dev/null
    fi
fi

# Past this point: root, running from a real file, fd 0 attached to TTY
# (or /dev/null in env-var mode).

# ---------------------------------------------------------------------------
# Uninstall mode: bash install.sh uninstall  OR  curl ... | bash -s uninstall
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "uninstall" ]]; then
    printf '\033[1;36m[install]\033[0m %s\n' "Uninstalling OpsBridge..."
    if command -v opsbridge >/dev/null 2>&1; then
        opsbridge uninstall --yes
    else
        for f in /etc/sudoers.d/opsbridge-agent \
                  /etc/ssh/sshd_config.d/50-opsbridge-agent.conf \
                  /etc/ssh/sshd_config.d/50-opsbridge-agent.conf.disabled \
                  /usr/local/bin/opsbridge \
                  /usr/local/bin/opsbridge-agent; do
            if [[ -e "$f" || -L "$f" ]]; then
                rm -f "$f"
                printf '\033[1;36m[install]\033[0m  removed %s\n' "$f"
            fi
        done
        rm -rf /opt/opsbridge /etc/opsbridge
        if id agent >/dev/null 2>&1; then
            userdel -r agent 2>/dev/null || true
            printf '\033[1;36m[install]\033[0m  removed user agent\n'
        fi
        if command -v systemctl >/dev/null 2>&1; then
            systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
        elif command -v service >/dev/null 2>&1; then
            service ssh reload 2>/dev/null || true
        fi
    fi
    rm -rf /opt/opsbridge-src
    printf '\033[1;36m[install]\033[0m  removed /opt/opsbridge-src\n'
    printf '\033[1;36m[install]\033[0m done.\n'
    exit 0
fi

REPO_URL="${OPSBRIDGE_REPO_URL:-https://github.com/cheney-yan/OpsBridge.git}"
REPO_REF="${1:-${OPSBRIDGE_REPO_REF:-main}}"
SRC_DIR="${OPSBRIDGE_SRC_DIR:-/opt/opsbridge-src}"
SUPPORTS_TTY=0
[[ -t 0 ]] && SUPPORTS_TTY=1

log()   { printf '\033[1;36m[install]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[install]\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[1;31m[install]\033[0m %s\n' "$*" >&2; exit 1; }

# --- 1. platform detection ----------------------------------------------------
KERNEL=$(uname -s)
case "$KERNEL" in
    Linux)  PLATFORM=linux ;;
    Darwin) PLATFORM=macos ;;
    *)      die "OpsBridge supports Linux and macOS. Detected: $KERNEL." ;;
esac
log "platform: $PLATFORM"

# --- 1b. ensure sshd is installed (Linux) -------------------------------------
if [[ "$PLATFORM" == "linux" ]]; then
    if [[ ! -x /usr/sbin/sshd ]]; then
        log "installing openssh-server ..."
        if command -v apt-get >/dev/null 2>&1; then
            DEBIAN_FRONTEND=noninteractive apt-get update -q
            DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssh-server
        elif command -v dnf >/dev/null 2>&1; then
            dnf install -y openssh-server
        elif command -v yum >/dev/null 2>&1; then
            yum install -y openssh-server
        else
            die "openssh-server missing and no recognized package manager. Install it manually and re-run."
        fi
    fi
    if command -v systemctl >/dev/null 2>&1; then
        systemctl enable --now ssh 2>/dev/null || systemctl enable --now sshd 2>/dev/null || \
            warn "could not enable ssh via systemctl — check manually"
    fi
fi

# --- 1c. ensure Node.js + npm are installed -----------------------------------
_ensure_nodejs() {
    if command -v npm >/dev/null 2>&1; then
        log "npm: $(command -v npm) ($(npm --version))"
        return 0
    fi
    log "npm not found — installing Node.js ..."
    if [[ "$PLATFORM" == "linux" ]]; then
        if command -v apt-get >/dev/null 2>&1; then
            DEBIAN_FRONTEND=noninteractive apt-get update -q
            DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends nodejs npm
        elif command -v dnf >/dev/null 2>&1; then
            dnf install -y nodejs npm
        elif command -v yum >/dev/null 2>&1; then
            yum install -y nodejs npm
        else
            die "npm missing and no recognized package manager. Install Node.js manually: https://nodejs.org/"
        fi
    elif [[ "$PLATFORM" == "macos" ]]; then
        if command -v brew >/dev/null 2>&1; then
            brew install node
        else
            die "npm missing. Install Node.js: brew install node  or  https://nodejs.org/"
        fi
    fi
    if ! command -v npm >/dev/null 2>&1; then
        die "npm install failed. Install Node.js manually: https://nodejs.org/"
    fi
    log "npm: $(command -v npm) ($(npm --version))"
}
_ensure_nodejs

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
        if ! read -r CHOICE 2>/dev/null; then
            CHOICE="k"
        fi
        CHOICE=${CHOICE:-k}
    else
        CHOICE="k"
    fi
    case "$CHOICE" in
        k|K) log "keeping existing config" ;;
        r|R) log "will re-prompt for provider/model/api key" ;;
        a|A) die "aborted" ;;
        *)   log "unknown choice $CHOICE — defaulting to [k]" ; CHOICE="k" ;;
    esac
fi

# --- 3. acquire source --------------------------------------------------------
log "acquiring source → $SRC_DIR"
# Allow root to operate on repos owned by another user (re-installs, ownership drift).
git config --global --add safe.directory "$SRC_DIR" 2>/dev/null || true
if [[ -d "$SRC_DIR/.git" ]]; then
    if ! git -C "$SRC_DIR" fetch --depth 1 origin "$REPO_REF" 2>/dev/null; then
        warn "git fetch failed — re-cloning from scratch"
        rm -rf "$SRC_DIR"
        git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$SRC_DIR"
    else
        git -C "$SRC_DIR" checkout FETCH_HEAD || git -C "$SRC_DIR" checkout "$REPO_REF" || true
    fi
elif command -v git >/dev/null 2>&1; then
    rm -rf "$SRC_DIR"
    if ! git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$SRC_DIR" 2>/dev/null; then
        log "  (REPO_REF=$REPO_REF not a branch/tag, falling back to full clone)"
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

# --- 4. bootstrap.sh — Python venv + admin CLI --------------------------------
log "bootstrap (uv + venv) ..."
"$SRC_DIR/bootstrap.sh"

# ---------------------------------------------------------------------------
# Helper functions for interactive prompts
# ---------------------------------------------------------------------------

# Read a secret with visible `*` echo per character.
read_secret_starred() {
    local prompt="$1"
    local secret="" char
    printf '%s' "$prompt"
    while IFS= read -r -s -n1 char; do
        if [[ -z "$char" ]]; then
            break
        fi
        case "$char" in
            $'\x7f'|$'\b')
                if [[ -n "$secret" ]]; then
                    secret="${secret%?}"
                    printf '\b \b'
                fi
                ;;
            *)
                secret+="$char"
                printf '*'
                ;;
        esac
    done
    printf '\n'
    REPLY="$secret"
}

# Mask a secret for confirmation echo.
_mask() {
    local s="$1" n=${#1}
    if (( n > 8 )); then
        printf '%s…%s (%d chars)' "${s:0:4}" "${s: -4}" "$n"
    else
        printf '(%d chars)' "$n"
    fi
}

prompt_for_config() {
    echo
    log "Configure model"

    local provider

    while :; do
        read -r -p "Provider [anthropic/openai] [anthropic]: " provider
        provider=${provider:-anthropic}
        case "$provider" in
            anthropic|openai) break ;;
            *) echo "  must be 'openai' or 'anthropic'" >&2 ;;
        esac
    done
    export OPSBRIDGE_PROVIDER="$provider"

    local base_url
    read -r -p "Custom base URL (empty = official endpoint): " base_url
    export OPSBRIDGE_BASE_URL="${base_url:-}"

    while :; do
        read_secret_starred "Paste API key: "
        if [[ -z "$REPLY" ]]; then
            echo "  API key cannot be empty" >&2
            continue
        fi
        export OPSBRIDGE_API_KEY="$REPLY"
        echo "  captured: $(_mask "$OPSBRIDGE_API_KEY")  → /etc/opsbridge/agent/api.key"
        break
    done
    # Model discovery and selection is handled by: opsbridge install
}

show_config_review() {
    echo
    log "Configured (review before applying):"
    printf '  %-12s : %s\n' "provider" "${OPSBRIDGE_PROVIDER:-}"
    printf '  %-12s : %s\n' "base url" "${OPSBRIDGE_BASE_URL:-(vendor default)}"
    printf '  %-12s : %s\n' "API key"  "$(_mask "$OPSBRIDGE_API_KEY")"
    printf '  %-12s : %s\n' "pubkey"   "${OPSBRIDGE_PUBKEY:-(skipped — add manually later)}"
    printf '  %-12s : %s\n' "model"    "${OPSBRIDGE_MODEL:-(selected during: opsbridge install)}"
    echo
}

check_llm_endpoint() {
    [[ "${OPSBRIDGE_SKIP_LLM_CHECK:-0}" == "1" ]] && return 0

    local provider="$OPSBRIDGE_PROVIDER"
    local model="$OPSBRIDGE_MODEL"
    local key="$OPSBRIDGE_API_KEY"
    local base="${OPSBRIDGE_BASE_URL:-}"
    local body http url
    body=$(mktemp)

    if [[ "$provider" == "openai" || -n "$base" ]]; then
        url="${base:-https://api.openai.com/v1}"
        url="${url%/}/chat/completions"
        log "verifying endpoint: $url (model=$model) ..."
        http=$(curl -sS -o "$body" -w "%{http_code}" -m 15 \
            -H "Authorization: Bearer $key" \
            -H "Content-Type: application/json" \
            -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"ok\"}],\"max_tokens\":4}" \
            "$url" 2>/dev/null || echo "000")
    else
        url="https://api.anthropic.com/v1/messages"
        log "verifying endpoint: $url (model=$model) ..."
        http=$(curl -sS -o "$body" -w "%{http_code}" -m 15 \
            -H "x-api-key: $key" \
            -H "anthropic-version: 2023-06-01" \
            -H "Content-Type: application/json" \
            -d "{\"model\":\"$model\",\"max_tokens\":4,\"messages\":[{\"role\":\"user\",\"content\":\"ok\"}]}" \
            "$url" 2>/dev/null || echo "000")
    fi

    if [[ "$http" == "200" ]]; then
        log "  ok (HTTP 200) — model recognized"
        rm -f "$body"
        return 0
    fi

    warn "LLM endpoint check failed: HTTP $http"
    head -c 600 "$body" >&2
    echo >&2
    rm -f "$body"
    return 1
}

prompt_for_pubkey() {
    [[ -n "${OPSBRIDGE_PUBKEY:-}" ]] && return 0
    echo
    log "Authorize an SSH pubkey"
    echo "Paste the full pubkey line, then press Enter. Empty to skip."
    local pubkey
    read -r -p "> " pubkey
    [[ -n "$pubkey" ]] && export OPSBRIDGE_PUBKEY="$pubkey"
}

# --- 5. interactive prompts ---------------------------------------------------
NEEDS_PROMPT=1
if [[ "$EXISTING_INSTALL" -eq 1 && "$CHOICE" =~ ^[kK]$ ]]; then
    NEEDS_PROMPT=0
fi
if [[ -n "${OPSBRIDGE_API_KEY:-}" ]]; then
    NEEDS_PROMPT=0
    log "OPSBRIDGE_API_KEY present — non-interactive env-var install"
fi
if [[ "$SUPPORTS_TTY" -eq 0 && -z "${OPSBRIDGE_API_KEY:-}" ]]; then
    die "no TTY and no OPSBRIDGE_API_KEY — set env vars (see install.sh header) or run with a TTY"
fi
if [[ "$NEEDS_PROMPT" -eq 1 ]]; then
    prompt_for_config
    show_config_review
    prompt_for_pubkey
elif [[ -n "${OPSBRIDGE_API_KEY:-}" ]]; then
    show_config_review
    if ! check_llm_endpoint; then
        warn "Continuing despite failed probe (env-var mode). Fix later with:"
        warn "    sudo opsbridge config"
    fi
fi

# --- 6. opsbridge install -----------------------------------------------------
ARGS=()
if [[ "$EXISTING_INSTALL" -eq 1 && "$CHOICE" =~ ^[kK]$ ]]; then
    ARGS+=("--skip-model-config")
elif [[ "$EXISTING_INSTALL" -eq 1 && "$CHOICE" =~ ^[rR]$ ]]; then
    ARGS+=("--reconfigure")
fi

log "running: opsbridge install ${ARGS[*]:-(no args)}"
/usr/local/bin/opsbridge install "${ARGS[@]}"

log "done."
log "  config + api.key  → /etc/opsbridge/agent/"
log "  system prompt     → /home/agent/.pi/agent/SYSTEM.md"
log "  launcher script   → /usr/local/bin/opsbridge-agent"
log "next: ssh agent@\$(hostname)"
