#!/usr/bin/env bash
# OpsBridge one-liner installer.
#
# Recommended (interactive):
#     curl -fsSL .../install.sh | bash         ← no leading sudo
#
# Non-interactive (CI / Ansible):
#     OPSBRIDGE_PROVIDER=anthropic \
#     OPSBRIDGE_MODEL=claude-sonnet-4-6 \
#     OPSBRIDGE_API_KEY=... \
#     OPSBRIDGE_PUBKEY="ssh-ed25519 AAAA... me@host" \
#     curl -fsSL .../install.sh | bash
#
# Why no leading sudo: `curl | sudo bash` is fundamentally broken for
# interactive prompts. sudo's stdin is the curl pipe (not your terminal),
# so under Ubuntu's `Defaults use_pty` the child pty has no path back to
# you — `read`, `getpass`, and friends all see EOF or block forever. This
# script handles sudo internally so its sudo invocation can attach to your
# REAL TTY.
#
# Re-running is safe — choose [k]eep / [r]econfigure / [a]bort on prompt.
set -euo pipefail

# -----------------------------------------------------------------------------
# Step 0: get the script onto disk + re-exec as root with a real TTY.
# -----------------------------------------------------------------------------
#
# Why this is one big atomic block: when we're piped (`curl | bash`), bash
# reads its script from stdin. The `cat > tmpfile` below slurps the REMAINDER
# of stdin into a file. After cat exits, stdin is EOF and bash has nothing
# more to read — it would just quietly exit without ever running anything
# else in this file. So the slurp + sudo re-exec MUST be the last thing in
# the if-piped branch.
#
# `exec sudo -E bash <tmpfile> </dev/tty` is the magic: sudo is invoked from
# our current shell (not from a pipe), so its stdin is the operator's real
# TTY, and sudo's child pty (under `Defaults use_pty`) wires up correctly to
# the terminal. Subsequent prompts in bash and Python both work.

# sudoers `Defaults env_reset` strips most env vars by default; `sudo -E`
# also obeys env_check/env_delete, so OPSBRIDGE_* vars wouldn't make it
# through. List them explicitly via --preserve-env.
_OB_ENV_KEEP=OPSBRIDGE_PROVIDER,OPSBRIDGE_MODEL,OPSBRIDGE_BASE_URL,OPSBRIDGE_API_KEY,OPSBRIDGE_JINA_API_KEY,OPSBRIDGE_PUBKEY,OPSBRIDGE_REPO_URL,OPSBRIDGE_REPO_REF,OPSBRIDGE_SRC_DIR,OPSBRIDGE_STDERR,OPSBRIDGE_USE_SYSTEM_PYTHON

if [[ -z "${BASH_SOURCE[0]:-}" ]]; then
    # We were piped. Slurp the rest of stdin and re-exec in one shot.
    _tmp=$(mktemp /tmp/opsbridge-install.XXXXXX.sh)
    cat > "$_tmp"
    chmod +x "$_tmp"
    if [[ "$(id -u)" -eq 0 ]]; then
        # Already root (legacy `curl | sudo bash`). The current pty is
        # detached from any usable input source (the curl pipe is gone),
        # so interactive prompts will see EOF. Env-var mode still works.
        if (: </dev/tty) 2>/dev/null; then
            exec bash "$_tmp" "$@" </dev/tty
        else
            exec bash "$_tmp" "$@" </dev/null
        fi
    else
        # Not root: re-elevate. sudo invoked from THIS shell (not a pipe),
        # with our TTY as stdin, so sudo's child pty is properly connected.
        if (: </dev/tty) 2>/dev/null; then
            exec sudo --preserve-env="$_OB_ENV_KEEP" bash "$_tmp" "$@" </dev/tty
        else
            exec sudo --preserve-env="$_OB_ENV_KEEP" bash "$_tmp" "$@" </dev/null
        fi
    fi
fi

# We're running from a real file (not a pipe). If we're not root yet
# (i.e., the user invoked `bash install.sh` directly), sudo-elevate now.
if [[ "$(id -u)" -ne 0 ]]; then
    if (: </dev/tty) 2>/dev/null; then
        exec sudo --preserve-env="$_OB_ENV_KEEP" bash "$0" "$@" </dev/tty
    else
        exec sudo --preserve-env="$_OB_ENV_KEEP" bash "$0" "$@" </dev/null
    fi
fi

# Past this point: root, running from a real file, fd 0 attached to TTY
# (or /dev/null in env-var mode).

REPO_URL="${OPSBRIDGE_REPO_URL:-https://github.com/cheney-yan/OpsBridge.git}"
REPO_REF="${OPSBRIDGE_REPO_REF:-main}"
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
    *)      die "OpsBridge supports Linux and macOS. Detected: $KERNEL. File an issue if you need BSD/other support." ;;
esac
log "platform: $PLATFORM"

# --- 1b. ensure sshd is installed (Linux) -------------------------------------
# Fresh containers (OrbStack, lxc, etc.) frequently ship without
# openssh-server; the agent's whole reachability story depends on it.
# Install + enable now, before anything else touches /etc/ssh.
if [[ "$PLATFORM" == "linux" ]]; then
    if [[ ! -x /usr/sbin/sshd ]]; then
        log "installing openssh-server (required for agent SSH access) ..."
        if command -v apt-get >/dev/null 2>&1; then
            DEBIAN_FRONTEND=noninteractive apt-get update -q
            DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssh-server
        elif command -v dnf >/dev/null 2>&1; then
            dnf install -y openssh-server
        elif command -v yum >/dev/null 2>&1; then
            yum install -y openssh-server
        else
            die "openssh-server is missing and I don't recognize this distro's package manager (no apt/dnf/yum). Install it manually and re-run."
        fi
    fi
    if command -v systemctl >/dev/null 2>&1; then
        # Enable + start the service. `ssh` is the unit on Debian/Ubuntu;
        # `sshd` on Fedora/RHEL. Try both, ignore "not found".
        systemctl enable --now ssh 2>/dev/null || systemctl enable --now sshd 2>/dev/null || \
            warn "could not enable ssh via systemctl — check manually"
    fi
fi

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

# --- 5. interactive prompts (in bash, not Python) -----------------------------
# Python's getpass.getpass() re-opens /dev/tty with its own quirks that
# misbehave in nested sudo+pty contexts. We sidestep all of that by gathering
# config in bash and feeding `opsbridge install` via env vars (the existing
# non-interactive code path).

# Read a secret with visible `*` echo per character (instead of `read -s`'s
# total silence). Supports Backspace (erase one `*`). Result goes into the
# global REPLY variable, mirroring bash's stock `read`.
read_secret_starred() {
    local prompt="$1"
    local secret="" char
    printf '%s' "$prompt"
    # `IFS=` keeps leading whitespace; -r preserves backslashes; -s suppresses
    # bash's own echo; -n1 reads ONE char. On Enter, bash strips the \n and
    # read returns empty, which we treat as end-of-input.
    while IFS= read -r -s -n1 char; do
        if [[ -z "$char" ]]; then
            break
        fi
        case "$char" in
            $'\x7f'|$'\b')
                # DEL or BS — erase one char from buffer + one `*` from screen.
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

# Query the configured LLM endpoint for its `/v1/models` list. Echoes one
# model id per line on stdout. Exits non-zero (and produces empty output)
# if the call fails — caller falls back to typing the name manually.
discover_models() {
    local base="${OPSBRIDGE_BASE_URL:-}"
    local provider="$OPSBRIDGE_PROVIDER"
    local key="$OPSBRIDGE_API_KEY"

    if [[ -z "$base" ]]; then
        if [[ "$provider" == "openai" ]]; then
            base="https://api.openai.com/v1"
        else
            # Anthropic vendor doesn't expose an OpenAI-style /v1/models that
            # we'd want to depend on; return a hardcoded short-list instead.
            printf 'claude-haiku-4-5\nclaude-sonnet-4-6\nclaude-opus-4-7\n'
            return 0
        fi
    fi
    base="${base%/}"

    local resp
    resp=$(curl -fsS -m 10 -H "Authorization: Bearer $key" "$base/models" 2>/dev/null) || return 1
    [[ -z "$resp" ]] && return 1

    # Pull model ids out of the JSON. Python is reliable; grep is the fallback.
    if command -v python3 >/dev/null 2>&1; then
        printf '%s' "$resp" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    items = data.get("data", data) if isinstance(data, dict) else data
    for m in items or []:
        mid = m.get("id") or m.get("name") if isinstance(m, dict) else m
        if mid:
            print(mid)
except Exception:
    pass
'
    else
        printf '%s' "$resp" | grep -oE '"id"[[:space:]]*:[[:space:]]*"[^"]+"' | sed 's/.*"\([^"]\+\)".*/\1/'
    fi
}

# Pick a default model id from a list based on provider hints. Falls back
# to the first entry if no hint matches, or to a hardcoded provider default
# if the list is empty.
default_model_from_list() {
    local provider="$1"
    local models="$2"
    local pick=""
    case "$provider" in
        anthropic)
            pick=$(printf '%s\n' "$models" | grep -E '^claude-sonnet' | tail -1)
            [[ -z "$pick" ]] && pick=$(printf '%s\n' "$models" | grep -iE 'sonnet' | head -1)
            ;;
        openai)
            pick=$(printf '%s\n' "$models" | grep -E '^gpt-[0-9].*mini$' | tail -1)
            [[ -z "$pick" ]] && pick=$(printf '%s\n' "$models" | grep -E '^gpt-' | head -1)
            ;;
    esac
    [[ -z "$pick" ]] && pick=$(printf '%s\n' "$models" | head -1)
    if [[ -z "$pick" ]]; then
        case "$provider" in
            openai)    pick="gpt-4.1-mini" ;;
            anthropic) pick="claude-sonnet-4-6" ;;
        esac
    fi
    echo "$pick"
}

# Ask the operator to pick a model, after we have URL + key (so we can
# discover what the endpoint actually serves). Falls back gracefully to
# free-text entry when discovery fails.
prompt_for_model() {
    local provider="$OPSBRIDGE_PROVIDER"
    local models default

    log "discovering models from ${OPSBRIDGE_BASE_URL:-the vendor's default endpoint} ..."
    models=$(discover_models 2>/dev/null) || true
    if [[ -z "$models" ]]; then
        warn "  couldn't fetch /v1/models — type a model id manually."
        case "$provider" in
            openai)    default="gpt-4.1-mini" ;;
            anthropic) default="claude-sonnet-4-6" ;;
        esac
        read -r -p "Model [$default]: " model
        OPSBRIDGE_MODEL="${model:-$default}"
        export OPSBRIDGE_MODEL
        return 0
    fi

    default=$(default_model_from_list "$provider" "$models")
    echo
    log "Available models (numbered list, recommended starred):"
    local arr=() i=1 m
    while IFS= read -r m; do
        [[ -z "$m" ]] && continue
        arr+=("$m")
        if [[ "$m" == "$default" ]]; then
            printf '  %2d. %s ★ recommended\n' "$i" "$m"
        else
            printf '  %2d. %s\n' "$i" "$m"
        fi
        ((i++))
        [[ "$i" -gt 15 ]] && { printf '  ... (%d more)\n' $(($(printf '%s\n' "$models" | wc -l) - 15)); break; }
    done <<< "$models"
    echo
    local choice
    read -r -p "Pick a number, or paste a model id [default: $default]: " choice
    if [[ -z "$choice" ]]; then
        OPSBRIDGE_MODEL="$default"
    elif [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#arr[@]} )); then
        OPSBRIDGE_MODEL="${arr[$((choice-1))]}"
    else
        OPSBRIDGE_MODEL="$choice"
    fi
    export OPSBRIDGE_MODEL
    echo "  selected: $OPSBRIDGE_MODEL"
}

prompt_for_config() {
    echo
    log "Configure LLM"

    local provider base_url jina_key

    # 1. Provider — drives default base URL + model-list discovery path.
    while :; do
        read -r -p "Provider [anthropic/openai] [anthropic]: " provider
        provider=${provider:-anthropic}
        case "$provider" in
            anthropic|openai) break ;;
            *) echo "  must be 'openai' or 'anthropic'" >&2 ;;
        esac
    done
    export OPSBRIDGE_PROVIDER="$provider"

    # 2. Base URL — optional, empty = vendor's official endpoint.
    read -r -p "Custom base URL (empty = official): " base_url
    export OPSBRIDGE_BASE_URL="$base_url"

    # 3. API key — required, hidden with per-char `*` echo so the operator
    # sees their keystrokes land. After Enter we ALSO show a masked summary
    # of the captured value, since the file it'll land in is separate from
    # config.toml and a silent capture invites "did it even save?".
    while :; do
        read_secret_starred "Paste API key: "
        if [[ -z "$REPLY" ]]; then
            echo "  API key cannot be empty" >&2
            continue
        fi
        export OPSBRIDGE_API_KEY="$REPLY"
        echo "  captured: $(_mask "$OPSBRIDGE_API_KEY")  → /etc/opsbridge/agent/api.key (mode 0400, owned by agent)"
        break
    done

    # 4. Model — now that we have URL + key, query /v1/models and offer
    # a numbered pick instead of asking the operator to remember names.
    prompt_for_model

    # 5. Jina key — optional, hidden, also with `*` echo.
    read_secret_starred "Jina API key for 'visit' (empty = skip): "
    export OPSBRIDGE_JINA_API_KEY="$REPLY"
    if [[ -n "$OPSBRIDGE_JINA_API_KEY" ]]; then
        echo "  captured: $(_mask "$OPSBRIDGE_JINA_API_KEY")  → /etc/opsbridge/agent/config.toml [visit]"
    fi
}

# Mask a secret for confirmation echo. Shows `aaaa…zzzz (N chars)` for
# secrets >8 chars long, `(N chars)` otherwise. Length is informative; the
# 4-char head/tail lets the operator visually spot a copy/paste truncation.
_mask() {
    local s="$1" n=${#1}
    if (( n > 8 )); then
        printf '%s…%s (%d chars)' "${s:0:4}" "${s: -4}" "$n"
    else
        printf '(%d chars)' "$n"
    fi
}

show_config_review() {
    echo
    log "Configured (review before applying):"
    printf '  %-12s : %s\n' "provider" "$OPSBRIDGE_PROVIDER"
    printf '  %-12s : %s\n' "model"    "$OPSBRIDGE_MODEL"
    printf '  %-12s : %s\n' "base url" "${OPSBRIDGE_BASE_URL:-(vendor default)}"
    printf '  %-12s : %s\n' "API key"  "$(_mask "$OPSBRIDGE_API_KEY")"
    printf '  %-12s : %s\n' "Jina key" "${OPSBRIDGE_JINA_API_KEY:+$(_mask "$OPSBRIDGE_JINA_API_KEY")}${OPSBRIDGE_JINA_API_KEY:-(skipped — 20 RPM unauthenticated)}"
    printf '  %-12s : %s\n' "pubkey"   "${OPSBRIDGE_PUBKEY:-(skipped — add manually later)}"
    echo
}

# Verify the LLM endpoint with a tiny round-trip BEFORE writing config files.
# Cheaper to catch a bad model/URL/key now than during the operator's first
# real TUI turn. Honors OPSBRIDGE_SKIP_LLM_CHECK=1 for offline/CI installs.
check_llm_endpoint() {
    [[ "${OPSBRIDGE_SKIP_LLM_CHECK:-0}" == "1" ]] && return 0

    local base="$OPSBRIDGE_BASE_URL" provider="$OPSBRIDGE_PROVIDER"
    local model="$OPSBRIDGE_MODEL" key="$OPSBRIDGE_API_KEY"
    local body http url
    body=$(mktemp)

    # When base_url is set, our model.py routes everything via the OpenAI
    # client (LiteLLM uses "openai/" prefix). So `/chat/completions` works
    # regardless of which vendor the proxy is fronting.
    if [[ -n "$base" ]] || [[ "$provider" == "openai" ]]; then
        url="${base:-https://api.openai.com/v1}"
        url="${url%/}/chat/completions"
        log "verifying LLM endpoint: $url (model=$model) ..."
        http=$(curl -sS -o "$body" -w "%{http_code}" -m 15 \
            -H "Authorization: Bearer $key" \
            -H "Content-Type: application/json" \
            -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"ok\"}],\"max_tokens\":4}" \
            "$url" 2>/dev/null || echo "000")
    else
        # Vendor-native Anthropic: x-api-key header, /v1/messages endpoint.
        url="https://api.anthropic.com/v1/messages"
        log "verifying LLM endpoint: $url (model=$model) ..."
        http=$(curl -sS -o "$body" -w "%{http_code}" -m 15 \
            -H "x-api-key: $key" \
            -H "anthropic-version: 2023-06-01" \
            -H "Content-Type: application/json" \
            -d "{\"model\":\"$model\",\"max_tokens\":4,\"messages\":[{\"role\":\"user\",\"content\":\"ok\"}]}" \
            "$url" 2>/dev/null || echo "000")
    fi

    if [[ "$http" == "200" ]]; then
        log "  ok (HTTP 200) — endpoint reachable, model recognized"
        rm -f "$body"
        return 0
    fi

    warn "LLM endpoint check failed: HTTP $http"
    echo "Response body:" >&2
    head -c 600 "$body" >&2
    echo >&2
    echo >&2
    echo "Common fixes:" >&2
    echo "  - base URL usually needs the /v1 suffix (e.g. https://your.proxy/v1)" >&2
    echo "  - verify model name with: curl ${url%/chat/completions}/models -H 'Authorization: Bearer YOUR_KEY' | head" >&2
    echo "  - confirm API key isn't typo'd or revoked" >&2
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

# Skip interactive prompts when re-running with [k]eep, or when env vars
# already cover the config, or in non-TTY environments.
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
    # Interactive: loop on the LLM probe so a wrong model/URL gets fixed
    # before any /etc files are written.
    while :; do
        prompt_for_config
        show_config_review
        if check_llm_endpoint; then
            break
        fi
        echo
        warn "Endpoint didn't accept the config above. Re-enter values, or Ctrl-C to abort."
        echo
    done
    prompt_for_pubkey
elif [[ -n "${OPSBRIDGE_API_KEY:-}" ]]; then
    # Env-var mode: probe once, surface failure prominently but proceed
    # (CI / Ansible has no human to re-prompt).
    show_config_review
    if ! check_llm_endpoint; then
        warn "Continuing despite the failed probe (env-var mode). Fix later with:"
        warn "    sudo opsbridge config && sudo opsbridge doctor --check-api"
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
log "  audit logs        → /var/log/opsbridge/agent/"
log "next: ssh agent@\$(hostname)"
