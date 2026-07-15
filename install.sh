#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/onhandserver}"
INSTALL_MODE=""
USE_SYSTEMD=true
NON_INTERACTIVE=false
UPGRADE_MODE=false
RECONFIGURE=false

usage() {
    cat <<EOF
Usage: sudo ./install.sh [options]

Interactive installer — copies files, creates .env, installs deps, and starts the service.

If already installed, detects bot vs agent, keeps .env and data files, updates code,
and restarts systemd. Choose "Reconfigure" to set everything up again.

Options:
  --dir PATH       Install directory (default: /opt/onhandserver)
  --upgrade        Update existing install, keep all config (non-interactive)
  --reconfigure    Force full setup again (overwrites .env)
  --bot            Non-interactive fresh install as Telegram bot
  --agent          Non-interactive fresh install as remote agent
  --no-systemd     Do not install or start a systemd service
  --systemd        Install and enable systemd service (default)
  -h, --help       Show this help

After git pull (recommended):
  sudo ./install.sh
  sudo ./install.sh --upgrade

Non-interactive bot env vars:
  TELEGRAM_BOT_TOKEN, ADMIN_CHAT_IDS
  Optional: SERVER_NAME, MONITOR_LOCAL, DISK_PATH, EXEC_ENABLED

Non-interactive agent env vars:
  AGENT_TOKEN (auto-generated if unset)
  Optional: DISK_PATH, AGENT_HOST, AGENT_PORT, EXEC_ENABLED
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir)
            INSTALL_DIR="$2"
            shift 2
            ;;
        --upgrade)
            UPGRADE_MODE=true
            NON_INTERACTIVE=true
            shift
            ;;
        --reconfigure)
            RECONFIGURE=true
            shift
            ;;
        --bot)
            INSTALL_MODE="bot"
            NON_INTERACTIVE=true
            shift
            ;;
        --agent)
            INSTALL_MODE="agent"
            NON_INTERACTIVE=true
            shift
            ;;
        --no-systemd)
            USE_SYSTEMD=false
            shift
            ;;
        --systemd)
            USE_SYSTEMD=true
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

prompt() {
    local var_name="$1"
    local prompt_text="$2"
    local default="${3:-}"
    local input=""
    if [[ -n "$default" ]]; then
        read -r -p "$prompt_text [$default]: " input
        input="${input:-$default}"
    else
        read -r -p "$prompt_text: " input
    fi
    printf -v "$var_name" '%s' "$input"
}

prompt_secret() {
    local var_name="$1"
    local prompt_text="$2"
    local input=""
    read -r -s -p "$prompt_text: " input
    echo
    printf -v "$var_name" '%s' "$input"
}

prompt_yes_no() {
    local prompt_text="$1"
    local default="${2:-y}"
    local hint="y/n"
    local input=""
    [[ "$default" == "y" ]] && hint="Y/n"
    [[ "$default" == "n" ]] && hint="y/N"
    read -r -p "$prompt_text ($hint): " input
    input="${input:-$default}"
    [[ "$input" =~ ^[Yy] ]]
}

env_bool_default() {
    local value="${1:-false}"
    if [[ "$value" == "true" || "$value" == "1" || "$value" == "yes" ]]; then
        echo "y"
    else
        echo "n"
    fi
}

prompt_exec_enabled() {
    local role="${1:-server}"
    local default
    default="$(env_bool_default "${EXEC_ENABLED:-false}")"
    echo ""
    echo "Shell command execution lets admins run commands on this ${role} from Telegram."
    if prompt_yes_no "Enable shell command execution?" "$default"; then
        EXEC_ENABLED="true"
    else
        EXEC_ENABLED="false"
    fi
}

set_env_var() {
    local key="$1"
    local value="$2"
    local env_file="${INSTALL_DIR}/.env"
    if grep -qE "^${key}=" "$env_file" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$env_file"
    else
        echo "${key}=${value}" >> "$env_file"
    fi
}

configure_exec_enabled_upgrade() {
    local role="server"
    [[ "$INSTALL_MODE" == "bot" ]] && role="bot server"
    [[ "$INSTALL_MODE" == "agent" ]] && role="agent host"

    if [[ "$NON_INTERACTIVE" == true ]]; then
        if [[ -n "${EXEC_ENABLED:-}" ]]; then
            set_env_var EXEC_ENABLED "$EXEC_ENABLED"
            echo "Set EXEC_ENABLED=${EXEC_ENABLED}"
        fi
        return 0
    fi

    prompt_exec_enabled "$role"
    set_env_var EXEC_ENABLED "$EXEC_ENABLED"
    echo "Set EXEC_ENABLED=${EXEC_ENABLED}"
}

generate_token() {
    python3 -c "import secrets; print(secrets.token_urlsafe(24))"
}

require_python() {
    if ! command -v python3 >/dev/null 2>&1; then
        echo "python3 is required. Install it first, e.g.:" >&2
        echo "  apt install python3 python3-venv python3-pip" >&2
        exit 1
    fi
}

is_installed() {
    [[ -d "${INSTALL_DIR}/venv" && -f "${INSTALL_DIR}/.env" ]]
}

detect_install_mode() {
    local env_file="${INSTALL_DIR}/.env"
    if [[ -f "$env_file" ]]; then
        if grep -qE '^TELEGRAM_BOT_TOKEN=' "$env_file" 2>/dev/null; then
            INSTALL_MODE="bot"
            return 0
        fi
        if grep -qE '^AGENT_TOKEN=' "$env_file" 2>/dev/null; then
            INSTALL_MODE="agent"
            return 0
        fi
    fi
    if systemctl is-enabled onhandserver &>/dev/null; then
        INSTALL_MODE="bot"
        return 0
    fi
    if systemctl is-enabled onhandserver-agent &>/dev/null; then
        INSTALL_MODE="agent"
        return 0
    fi
    return 1
}

load_existing_env() {
    local env_file="${INSTALL_DIR}/.env"
    if [[ ! -f "$env_file" ]]; then
        return 1
    fi
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
    return 0
}

describe_existing_data() {
    echo "  Preserved files:"
    echo "    • .env"
    [[ -f "${INSTALL_DIR}/allowed_users.json" ]] && echo "    • allowed_users.json"
    [[ -f "${INSTALL_DIR}/servers.json" ]] && echo "    • servers.json"
    [[ -f "${INSTALL_DIR}/monitor_config.json" ]] && echo "    • monitor_config.json"
}

copy_files() {
    echo ""
    echo "Installing files to ${INSTALL_DIR} ..."
    mkdir -p "${INSTALL_DIR}"
    cp "${SCRIPT_DIR}/bot.py" \
       "${SCRIPT_DIR}/agent.py" \
       "${SCRIPT_DIR}/allowed_users.py" \
       "${SCRIPT_DIR}/command_runner.py" \
       "${SCRIPT_DIR}/config_store.py" \
       "${SCRIPT_DIR}/servers_store.py" \
       "${SCRIPT_DIR}/remote_client.py" \
       "${SCRIPT_DIR}/system_stats.py" \
       "${SCRIPT_DIR}/requirements.txt" \
       "${INSTALL_DIR}/"

    if [[ ! -f "${INSTALL_DIR}/monitor_config.json" ]]; then
        cp "${SCRIPT_DIR}/monitor_config.json" "${INSTALL_DIR}/monitor_config.json"
        echo "  Created default monitor_config.json"
    else
        echo "  Kept existing monitor_config.json"
    fi
}

setup_venv() {
    if [[ -d "${INSTALL_DIR}/venv" ]]; then
        echo "Updating virtualenv ..."
        "${INSTALL_DIR}/venv/bin/pip" install --upgrade pip -q
        "${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q
    else
        echo "Creating virtualenv and installing dependencies ..."
        python3 -m venv "${INSTALL_DIR}/venv"
        "${INSTALL_DIR}/venv/bin/pip" install --upgrade pip -q
        "${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q
    fi
}

write_bot_env() {
    local env_file="${INSTALL_DIR}/.env"
    cat > "${env_file}" <<EOF
# OnHandServer — Telegram bot (generated by install.sh)
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
ADMIN_CHAT_IDS=${ADMIN_CHAT_IDS}
DISK_PATH=${DISK_PATH:-/}
SERVER_NAME=${SERVER_NAME:-}
MONITOR_LOCAL=${MONITOR_LOCAL:-true}
EXEC_ENABLED=${EXEC_ENABLED:-false}
EOF
    chmod 600 "${env_file}"
    echo "Wrote ${env_file}"
}

write_agent_env() {
    local env_file="${INSTALL_DIR}/.env"
    cat > "${env_file}" <<EOF
# OnHandServer — remote agent (generated by install.sh)
AGENT_TOKEN=${AGENT_TOKEN}
DISK_PATH=${DISK_PATH:-/}
AGENT_HOST=${AGENT_HOST:-0.0.0.0}
AGENT_PORT=${AGENT_PORT:-8765}
EXEC_ENABLED=${EXEC_ENABLED:-false}
EOF
    chmod 600 "${env_file}"
    echo "Wrote ${env_file}"
}

disable_other_service() {
    if [[ "$(id -u)" -ne 0 ]]; then
        return 0
    fi
    if [[ "$INSTALL_MODE" == "bot" ]]; then
        if systemctl is-enabled onhandserver-agent &>/dev/null; then
            systemctl disable --now onhandserver-agent 2>/dev/null || true
            echo "Disabled onhandserver-agent (switched to bot mode)"
        fi
    else
        if systemctl is-enabled onhandserver &>/dev/null; then
            systemctl disable --now onhandserver 2>/dev/null || true
            echo "Disabled onhandserver (switched to agent mode)"
        fi
    fi
}

install_systemd_service() {
    local service_name="$1"
    local unit_template="$2"

    if [[ "$(id -u)" -ne 0 ]]; then
        echo ""
        echo "Systemd install needs root. Re-run with:"
        echo "  sudo $0 --dir ${INSTALL_DIR}"
        return 1
    fi

    disable_other_service

    sed "s|/opt/onhandserver|${INSTALL_DIR}|g" "${SCRIPT_DIR}/${unit_template}" \
        > "/etc/systemd/system/${service_name}.service"

    systemctl daemon-reload
    systemctl enable "${service_name}"
    systemctl restart "${service_name}"
    echo ""
    echo "============================================"
    echo "  Service restarted"
    echo "============================================"
    systemctl --no-pager status "${service_name}" || true
}

run_bot_config_questions() {
    echo "--- Bot configuration ---"
    echo "Create a bot via @BotFather and paste the token below."
    echo "Get your chat ID from @userinfobot or tap My ID in the bot."
    echo ""
    while [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; do
        prompt_secret TELEGRAM_BOT_TOKEN "Telegram bot token"
        if [[ -z "$TELEGRAM_BOT_TOKEN" ]]; then
            echo "Token cannot be empty."
        fi
    done
    while [[ -z "${ADMIN_CHAT_IDS:-}" ]]; do
        prompt ADMIN_CHAT_IDS "Admin chat ID(s), comma-separated"
        if [[ -z "$ADMIN_CHAT_IDS" ]]; then
            echo "At least one admin chat ID is required."
        fi
    done
    prompt SERVER_NAME "Display name for this host (optional, Enter to skip)" ""
    if prompt_yes_no "Monitor this machine's resources?" "y"; then
        MONITOR_LOCAL="true"
    else
        MONITOR_LOCAL="false"
    fi
    prompt DISK_PATH "Disk path to monitor" "/"
    DISK_PATH="${DISK_PATH:-/}"
    SERVER_NAME="${SERVER_NAME:-}"
    prompt_exec_enabled "bot server"
}

run_agent_config_questions() {
    echo "--- Agent configuration ---"
    echo "This host will expose stats to the bot over HTTP."
    echo ""
    if prompt_yes_no "Generate a random agent token?" "y"; then
        AGENT_TOKEN="$(generate_token)"
        echo "Generated token: ${AGENT_TOKEN}"
        echo "(Save it — you need this when adding the server in the bot)"
    else
        while [[ -z "${AGENT_TOKEN:-}" ]]; do
            prompt_secret AGENT_TOKEN "Agent token (same value used when adding server in bot)"
            if [[ -z "$AGENT_TOKEN" ]]; then
                echo "Token cannot be empty."
            fi
        done
    fi
    prompt DISK_PATH "Disk path to monitor" "/"
    DISK_PATH="${DISK_PATH:-/}"
    prompt AGENT_HOST "Listen address" "0.0.0.0"
    AGENT_HOST="${AGENT_HOST:-0.0.0.0}"
    prompt AGENT_PORT "Listen port" "8765"
    AGENT_PORT="${AGENT_PORT:-8765}"
    prompt_exec_enabled "agent host"
}

run_interactive_setup() {
    echo "============================================"
    echo "  OnHandServer installer"
    echo "============================================"
    echo ""
    echo "Run with sudo when installing to ${INSTALL_DIR}."
    echo ""

    prompt INSTALL_DIR "Install directory" "${INSTALL_DIR}"

    if is_installed && [[ "$RECONFIGURE" == false ]]; then
        if detect_install_mode; then
            echo ""
            echo "Existing installation detected"
            echo "  Type:   ${INSTALL_MODE}"
            echo "  Path:   ${INSTALL_DIR}"
            describe_existing_data
            echo ""
            echo "What do you want to do?"
            echo "  1) Upgrade      — update code, keep all settings (after git pull)"
            echo "  2) Reconfigure  — set up again from scratch (overwrites .env)"
            echo ""
            local action=""
            while [[ "$action" != "1" && "$action" != "2" ]]; do
                prompt action "Enter 1 or 2" "1"
            done
            if [[ "$action" == "1" ]]; then
                UPGRADE_MODE=true
                USE_SYSTEMD=true
                echo ""
                echo "Upgrade mode: keeping existing configuration."
                return 0
            fi
            RECONFIGURE=true
        fi
    fi

    if [[ "$RECONFIGURE" == true && is_installed ]]; then
        echo ""
        echo "Reconfigure mode: .env will be replaced. Other data files are kept"
        echo "  (allowed_users.json, servers.json, monitor_config.json)."
        echo ""
    fi

    echo ""
    echo "What is this machine?"
    echo "  1) Bot server  — runs the Telegram bot (one per setup)"
    echo "  2) Agent       — reports stats from this host to the bot"
    echo ""
    local role=""
    while [[ "$role" != "1" && "$role" != "2" ]]; do
        prompt role "Enter 1 or 2"
    done

    if [[ "$role" == "1" ]]; then
        INSTALL_MODE="bot"
    else
        INSTALL_MODE="agent"
    fi

    echo ""
    if [[ "$INSTALL_MODE" == "bot" ]]; then
        run_bot_config_questions
    else
        run_agent_config_questions
    fi

    echo ""
    if prompt_yes_no "Install as systemd service and start now?" "y"; then
        USE_SYSTEMD=true
    else
        USE_SYSTEMD=false
    fi
}

run_noninteractive_setup() {
    if [[ "$UPGRADE_MODE" == true ]]; then
        if ! is_installed; then
            echo "No existing installation at ${INSTALL_DIR}." >&2
            echo "Run without --upgrade for a fresh install." >&2
            exit 1
        fi
        if ! detect_install_mode; then
            echo "Could not detect bot vs agent at ${INSTALL_DIR}." >&2
            exit 1
        fi
        load_existing_env || true
        USE_SYSTEMD=true
        echo "Upgrading ${INSTALL_MODE} at ${INSTALL_DIR} (keeping config) ..."
        return 0
    fi

    if [[ -z "$INSTALL_MODE" ]]; then
        echo "Non-interactive install requires --bot, --agent, or --upgrade." >&2
        exit 1
    fi

    if [[ "$INSTALL_MODE" == "bot" ]]; then
        TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
        ADMIN_CHAT_IDS="${ADMIN_CHAT_IDS:-}"
        if [[ -z "$TELEGRAM_BOT_TOKEN" || -z "$ADMIN_CHAT_IDS" ]]; then
            echo "Non-interactive bot install requires TELEGRAM_BOT_TOKEN and ADMIN_CHAT_IDS." >&2
            exit 1
        fi
        SERVER_NAME="${SERVER_NAME:-}"
        MONITOR_LOCAL="${MONITOR_LOCAL:-true}"
        DISK_PATH="${DISK_PATH:-/}"
        EXEC_ENABLED="${EXEC_ENABLED:-false}"
    else
        AGENT_TOKEN="${AGENT_TOKEN:-$(generate_token)}"
        DISK_PATH="${DISK_PATH:-/}"
        AGENT_HOST="${AGENT_HOST:-0.0.0.0}"
        AGENT_PORT="${AGENT_PORT:-8765}"
        EXEC_ENABLED="${EXEC_ENABLED:-false}"
    fi
}

# --- main ---

require_python

if [[ "$NON_INTERACTIVE" == false ]]; then
    run_interactive_setup
else
    run_noninteractive_setup
fi

if [[ -z "$INSTALL_MODE" ]]; then
    echo "Install mode not set." >&2
    exit 1
fi

copy_files
setup_venv

should_write_env=false
if [[ "$UPGRADE_MODE" == true ]]; then
    echo "Keeping existing .env"
    load_existing_env || true
    configure_exec_enabled_upgrade
elif [[ "$RECONFIGURE" == true || "$NON_INTERACTIVE" == true ]]; then
    should_write_env=true
elif [[ ! -f "${INSTALL_DIR}/.env" ]]; then
    should_write_env=true
else
    echo ""
    echo "Existing .env found."
    if prompt_yes_no "Overwrite .env?" "n"; then
        should_write_env=true
    else
        echo "Keeping existing .env"
        load_existing_env || true
    fi
fi

if [[ "$should_write_env" == true ]]; then
    if [[ "$INSTALL_MODE" == "bot" ]]; then
        write_bot_env
    else
        write_agent_env
    fi
else
    load_existing_env || true
fi

echo ""
systemd_ok=false
if [[ "$USE_SYSTEMD" == true ]]; then
    if [[ "$INSTALL_MODE" == "bot" ]]; then
        install_systemd_service "onhandserver" "onhandserver.service" && systemd_ok=true
    else
        install_systemd_service "onhandserver-agent" "onhandserver-agent.service" && systemd_ok=true
    fi
fi

if [[ "$UPGRADE_MODE" == true ]]; then
    echo ""
    echo "Upgrade complete — code updated, config unchanged, service restarted."
fi

if [[ "$USE_SYSTEMD" == false || "$systemd_ok" == false ]]; then
    echo "============================================"
    echo "  Install complete"
    echo "============================================"
    echo ""
    echo "Config: ${INSTALL_DIR}/.env"
    echo ""
    if [[ "$INSTALL_MODE" == "bot" ]]; then
        echo "Run manually:"
        echo "  cd ${INSTALL_DIR} && source venv/bin/activate && python bot.py"
        echo ""
        echo "Or install as a service:"
        echo "  sudo ./install.sh --dir ${INSTALL_DIR} --bot --systemd"
    else
        echo "Run manually:"
        echo "  cd ${INSTALL_DIR} && source venv/bin/activate && python agent.py"
        echo ""
        echo "Or install as a service:"
        echo "  sudo ./install.sh --dir ${INSTALL_DIR} --agent --systemd"
    fi
fi

if [[ "$INSTALL_MODE" == "agent" ]]; then
    load_existing_env 2>/dev/null || true
    if [[ -n "${AGENT_TOKEN:-}" && "$UPGRADE_MODE" == false && "$should_write_env" == true ]]; then
        echo ""
        echo "Agent token: ${AGENT_TOKEN}"
        echo "Register in bot: Manage servers → Add server"
        echo "  name | http://<this-ip>:${AGENT_PORT:-8765} | ${AGENT_TOKEN}"
        echo "Firewall: allow port ${AGENT_PORT:-8765} only from your bot server's IP."
    fi
fi

if [[ "$INSTALL_MODE" == "bot" && "$UPGRADE_MODE" == false ]]; then
    echo ""
    echo "Open Telegram and send /start to your bot."
fi
