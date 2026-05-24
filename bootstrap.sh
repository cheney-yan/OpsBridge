#!/usr/bin/env bash
# OpsBridge bootstrap — installs uv, builds the venv, symlinks the admin CLI.
# Idempotent. Re-running upgrades dependencies and refreshes the symlink.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "bootstrap.sh must be run as root" >&2
    exit 1
fi

SRC_DIR="${BASH_SOURCE[0]%/*}"
SRC_DIR="$(cd "$SRC_DIR" && pwd)"

PREFIX="/opt/opsbridge/agent"
PYTHON_DIR="$PREFIX/python"
VENV_DIR="$PREFIX/.venv"

# Python interpreter override (T1.1 + T1.2: --use-system-python path).
USE_SYSTEM_PYTHON="${OPSBRIDGE_USE_SYSTEM_PYTHON:-0}"

echo "[bootstrap] source dir: $SRC_DIR"
echo "[bootstrap] install prefix: $PREFIX"

# --- 1. install uv if missing -------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
    echo "[bootstrap] installing uv ..."
    # Astral's installer; honors UV_INSTALL_DIR. Place into /usr/local/bin.
    export UV_INSTALL_DIR="/usr/local/bin"
    export UV_UNMANAGED_INSTALL="${UV_INSTALL_DIR}"
    curl -fsSL https://astral.sh/uv/install.sh | sh
fi

UV_BIN="$(command -v uv)"
echo "[bootstrap] uv: $UV_BIN"

# --- 2. fetch a managed Python interpreter -----------------------------------
mkdir -p "$PREFIX"

if [[ "$USE_SYSTEM_PYTHON" == "1" ]]; then
    PYTHON_BIN="$(command -v python3)"
    echo "[bootstrap] using system python: $PYTHON_BIN"
else
    echo "[bootstrap] fetching managed Python 3.12 ..."
    # uv python install drops a standalone interpreter under UV_PYTHON_INSTALL_DIR.
    export UV_PYTHON_INSTALL_DIR="$PYTHON_DIR"
    "$UV_BIN" python install 3.12
    PYTHON_BIN="$("$UV_BIN" python find 3.12)"
    echo "[bootstrap] python: $PYTHON_BIN"
fi

# --- 3. build / refresh the venv ---------------------------------------------
if [[ ! -d "$VENV_DIR" ]]; then
    echo "[bootstrap] creating venv ..."
    "$UV_BIN" venv --python "$PYTHON_BIN" "$VENV_DIR"
fi

echo "[bootstrap] installing project ..."
VIRTUAL_ENV="$VENV_DIR" "$UV_BIN" pip install --python "$VENV_DIR/bin/python" \
    --reinstall "$SRC_DIR"

# --- 4. symlink the admin CLI ------------------------------------------------
LINK="/usr/local/bin/opsbridge"
TARGET="$VENV_DIR/bin/opsbridge"
if [[ ! -x "$TARGET" ]]; then
    echo "[bootstrap] ERROR: $TARGET missing after install" >&2
    exit 1
fi
ln -sfn "$TARGET" "$LINK"
echo "[bootstrap] symlinked $LINK -> $TARGET"

# --- 5. write the source dir for later use by `opsbridge install` ------------
# So the admin command knows where deploy/*.snippet live without env vars.
mkdir -p /etc/opsbridge
printf 'src_dir = "%s"\n' "$SRC_DIR" > /etc/opsbridge/bootstrap.toml
chmod 0644 /etc/opsbridge/bootstrap.toml

echo
echo "[bootstrap] done. Next: sudo opsbridge install"
