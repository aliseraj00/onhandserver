#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/onhandserver}"
SERVICE_NAME="onhandserver"

usage() {
    cat <<EOF
Usage: sudo ./install.sh [options]

Options:
  --dir PATH     Install directory (default: /opt/onhandserver)
  --systemd      Install and enable systemd service
  -h, --help     Show this help

Without --systemd, only copies files and creates the virtualenv.
EOF
}

install_systemd=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir)
            INSTALL_DIR="$2"
            shift 2
            ;;
        --systemd)
            install_systemd=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            exit 1
            ;;
    esac
done

if [[ "$install_systemd" == true && "$(id -u)" -ne 0 ]]; then
    echo "Run with sudo when using --systemd" >&2
    exit 1
fi

echo "Installing to ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
cp -r "${SCRIPT_DIR}/bot.py" \
      "${SCRIPT_DIR}/agent.py" \
      "${SCRIPT_DIR}/allowed_users.py" \
      "${SCRIPT_DIR}/config_store.py" \
      "${SCRIPT_DIR}/servers_store.py" \
      "${SCRIPT_DIR}/remote_client.py" \
      "${SCRIPT_DIR}/system_stats.py" \
      "${SCRIPT_DIR}/requirements.txt" \
      "${SCRIPT_DIR}/monitor_config.json" \
      "${INSTALL_DIR}/"

if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
    cp "${SCRIPT_DIR}/.env.example" "${INSTALL_DIR}/.env"
    echo "Created ${INSTALL_DIR}/.env — edit TELEGRAM_BOT_TOKEN and ADMIN_CHAT_IDS"
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required. Install it first, e.g.: apt install python3 python3-venv python3-pip" >&2
    exit 1
fi

python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

if [[ "$install_systemd" == true ]]; then
    sed "s|/opt/onhandserver|${INSTALL_DIR}|g" "${SCRIPT_DIR}/onhandserver.service" \
        > "/etc/systemd/system/${SERVICE_NAME}.service"
    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}"
    systemctl restart "${SERVICE_NAME}"
    echo "Service installed. Status: systemctl status ${SERVICE_NAME}"
else
    cat <<EOF

Install complete.

1. Edit ${INSTALL_DIR}/.env
2. Run manually:
   cd ${INSTALL_DIR}
   source venv/bin/activate
   python bot.py

3. Or install as a service:
   sudo INSTALL_DIR=${INSTALL_DIR} ./install.sh --systemd
EOF
fi
