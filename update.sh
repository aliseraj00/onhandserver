#!/usr/bin/env bash
# One-step upgrade on the server: pull latest code, then reinstall into /opt.
# Usage:
#   sudo ./update.sh
#   sudo EXEC_ENABLED=true ./update.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run as root: sudo ./update.sh" >&2
    exit 1
fi

if [[ ! -d .git ]]; then
    echo "Not a git repo: ${SCRIPT_DIR}" >&2
    echo "Clone the repo first, then run update.sh from that clone." >&2
    exit 1
fi

# Config lives under /opt/onhandserver — discard local clone edits so pull never stalls.
if ! git diff --quiet --ignore-submodules -- 2>/dev/null \
    || ! git diff --cached --quiet --ignore-submodules -- 2>/dev/null; then
    echo "Discarding local clone changes so git pull can proceed..."
    git reset --hard HEAD
    git clean -fd
fi

echo "Pulling latest..."
git pull --ff-only

# bash avoids needing +x if the filesystem/clone lost the execute bit
echo "Running install --upgrade..."
if [[ -n "${EXEC_ENABLED:-}" ]]; then
    exec env EXEC_ENABLED="${EXEC_ENABLED}" bash "${SCRIPT_DIR}/install.sh" --upgrade "$@"
fi
exec bash "${SCRIPT_DIR}/install.sh" --upgrade "$@"
