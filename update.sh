#!/usr/bin/env bash
# Upgrade OnHandServer: refresh source, then reinstall into /opt keeping config.
#
# From a local clone:
#   sudo ./update.sh
#
# One-liner (no clone needed):
#   bash <(curl -Ls https://raw.githubusercontent.com/aliseraj00/onhandserver/main/update.sh)
#
# Optional: sudo EXEC_ENABLED=true ./update.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_URL="${OHS_REPO_URL:-https://github.com/aliseraj00/onhandserver.git}"
REPO_BRANCH="${OHS_BRANCH:-main}"
SOURCE_DIR="${OHS_SOURCE_DIR:-/opt/onhandserver-src}"

has_local_source() {
    [[ -f "${SCRIPT_DIR}/bot.py" && -f "${SCRIPT_DIR}/agent.py" && -f "${SCRIPT_DIR}/install.sh" ]]
}

ensure_git() {
    if command -v git >/dev/null 2>&1; then
        return 0
    fi
    echo "git is required. Install it first, e.g.: apt install -y git" >&2
    exit 1
}

sync_source_repo() {
    ensure_git
    mkdir -p "$(dirname "${SOURCE_DIR}")"
    if [[ -d "${SOURCE_DIR}/.git" ]]; then
        echo "Updating source at ${SOURCE_DIR} ..."
        git -C "${SOURCE_DIR}" fetch --depth 1 origin "${REPO_BRANCH}"
        git -C "${SOURCE_DIR}" checkout -B "${REPO_BRANCH}" "FETCH_HEAD"
        git -C "${SOURCE_DIR}" reset --hard "FETCH_HEAD"
        git -C "${SOURCE_DIR}" clean -fd
    else
        echo "Cloning ${REPO_URL} (${REPO_BRANCH}) → ${SOURCE_DIR} ..."
        rm -rf "${SOURCE_DIR}"
        git clone --depth 1 --branch "${REPO_BRANCH}" "${REPO_URL}" "${SOURCE_DIR}"
    fi
}

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run as root: sudo ./update.sh" >&2
    echo "Or: bash <(curl -Ls https://raw.githubusercontent.com/aliseraj00/onhandserver/main/update.sh)" >&2
    exit 1
fi

# Curled / incomplete copy: bootstrap repo under SOURCE_DIR, then re-exec.
if ! has_local_source; then
    echo "No local source next to updater — bootstrapping from GitHub..."
    sync_source_repo
    echo "Continuing with ${SOURCE_DIR}/update.sh ..."
    if [[ -n "${EXEC_ENABLED:-}" ]]; then
        exec env EXEC_ENABLED="${EXEC_ENABLED}" bash "${SOURCE_DIR}/update.sh" "$@"
    fi
    exec bash "${SOURCE_DIR}/update.sh" "$@"
fi

cd "${SCRIPT_DIR}"

if [[ -d .git ]]; then
    # Config lives under /opt/onhandserver — discard local clone edits so pull never stalls.
    if ! git diff --quiet --ignore-submodules -- 2>/dev/null \
        || ! git diff --cached --quiet --ignore-submodules -- 2>/dev/null; then
        echo "Discarding local clone changes so git pull can proceed..."
        git reset --hard HEAD
        git clean -fd
    fi

    echo "Pulling latest..."
    git pull --ff-only
else
    echo "Not a git repo at ${SCRIPT_DIR}; using files as-is."
fi

# bash avoids needing +x if the filesystem/clone lost the execute bit
echo "Running install --upgrade..."
if [[ -n "${EXEC_ENABLED:-}" ]]; then
    exec env EXEC_ENABLED="${EXEC_ENABLED}" bash "${SCRIPT_DIR}/install.sh" --upgrade "$@"
fi
exec bash "${SCRIPT_DIR}/install.sh" --upgrade "$@"
