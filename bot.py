import asyncio
import logging
import os
import socket
import time
from datetime import datetime
from pathlib import Path

import psutil
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from allowed_users import AllowedUsersStore
from backup_util import (
    IMPORTANT_LINUX_LOGS,
    TELEGRAM_UPLOAD_LIMIT_BYTES,
    BackupError,
    classify_log_path,
    create_backup_zip,
    create_paths_zip,
    format_size,
    list_important_linux_logs,
)
from command_runner import CommandResult, format_command_result, run_command
from config_store import ConfigStore
from remote_client import (
    RemoteAgentError,
    fetch_remote_status,
    ping_agent,
    run_remote_command,
)
from servers_store import ServersStore
from system_stats import (
    ResourceSnapshot,
    format_status,
    format_top_processes_block,
    sample_resources,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent / "monitor_config.json"
ALLOWED_USERS_PATH = Path(__file__).resolve().parent / "allowed_users.json"
SERVERS_PATH = Path(__file__).resolve().parent / "servers.json"
config = ConfigStore(CONFIG_PATH)
allowed_users = AllowedUsersStore(ALLOWED_USERS_PATH)
servers = ServersStore(SERVERS_PATH)

LOCAL_SERVER_ID = "__local__"
MONITOR_LOCAL = os.getenv("MONITOR_LOCAL", "true").lower() in ("1", "true", "yes")
LOCAL_SERVER_NAME = os.getenv("SERVER_NAME", "").strip()
EXEC_TIMEOUT_SECONDS = float(os.getenv("EXEC_TIMEOUT_SECONDS", "30"))
EXEC_MAX_OUTPUT = int(os.getenv("EXEC_MAX_OUTPUT", "3500"))
EXEC_ENABLED = os.getenv("EXEC_ENABLED", "false").lower() in ("1", "true", "yes")

CB = "ohs"


def _parse_chat_ids(env_name: str) -> set[int]:
    return {
        int(chat_id.strip())
        for chat_id in os.getenv(env_name, "").split(",")
        if chat_id.strip()
    }


ADMIN_CHAT_IDS = _parse_chat_ids("ADMIN_CHAT_IDS") or _parse_chat_ids(
    "ALLOWED_CHAT_IDS"
)

_cpu_high_streak: dict[str, int] = {}
_last_alert_at: dict[str, float] = {}
_last_backup_at: float = 0.0
_backup_running: bool = False


def _allowed_chat_ids() -> set[int]:
    return ADMIN_CHAT_IDS | allowed_users.chat_ids


def _chat_id(update: Update) -> int | None:
    chat = update.effective_chat
    return chat.id if chat is not None else None


def _authorized(update: Update) -> bool:
    chat_id = _chat_id(update)
    return chat_id is not None and chat_id in _allowed_chat_ids()


def _is_admin(update: Update) -> bool:
    chat_id = _chat_id(update)
    return chat_id is not None and chat_id in ADMIN_CHAT_IDS


def _local_display_name() -> str:
    return LOCAL_SERVER_NAME or socket.gethostname()


def _monitor_target_ids() -> list[str]:
    targets: list[str] = []
    if MONITOR_LOCAL:
        targets.append(LOCAL_SERVER_ID)
    targets.extend(server["id"] for server in servers.ready_servers)
    return targets


def _server_label(server_id: str) -> str:
    if server_id == LOCAL_SERVER_ID:
        return _local_display_name()
    entry = servers.get(server_id)
    return entry["name"] if entry else server_id


def _resolve_selection(chat_id: int | None) -> str | None:
    if chat_id is not None:
        selected = servers.get_selection(chat_id)
        if selected:
            if selected == LOCAL_SERVER_ID:
                return selected
            entry = servers.get(selected)
            if entry and ServersStore.is_ready(entry):
                return selected
    targets = _monitor_target_ids()
    if len(targets) == 1:
        return targets[0]
    return None


async def _fetch_status(server_id: str) -> ResourceSnapshot:
    if server_id == LOCAL_SERVER_ID:
        return await asyncio.to_thread(
            sample_resources, config.data["disk_path"], 1.0
        )
    entry = servers.get(server_id)
    if entry is None:
        raise RemoteAgentError("Server not found")
    url = entry.get("url", "").strip()
    if not url:
        raise RemoteAgentError(
            f"{entry['name']} has no URL — remove it in Admin → Manage servers and re-add"
        )
    return await fetch_remote_status(url, entry["token"])


async def _execute_on_server(server_id: str, command: str) -> CommandResult:
    if server_id == LOCAL_SERVER_ID:
        return await asyncio.to_thread(
            run_command,
            command,
            timeout=EXEC_TIMEOUT_SECONDS,
            max_output=EXEC_MAX_OUTPUT,
        )
    entry = servers.get(server_id)
    if entry is None:
        raise RemoteAgentError("Server not found")
    url = entry.get("url", "").strip()
    if not url:
        raise RemoteAgentError(
            f"{entry['name']} has no URL — remove it and re-add with the agent URL"
        )
    return await run_remote_command(
        url,
        entry["token"],
        command,
        timeout=EXEC_TIMEOUT_SECONDS,
        max_output=EXEC_MAX_OUTPUT,
    )


def _exec_back_keyboard(server_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("◀️ Back", callback_data=_cb("pick", server_id))]]
    )


async def _show_server_actions(update: Update, server_id: str) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    label = _server_label(server_id)
    lines = [f"Server: {label}", "", "Choose an action:"]
    if _is_admin(update) and not EXEC_ENABLED:
        lines.append("\nShell commands are disabled (EXEC_ENABLED=false).")
    await _send_or_edit(
        update,
        "\n".join(lines),
        reply_markup=_server_actions_keyboard(server_id, update),
    )


async def _prompt_exec_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE | None,
    server_id: str,
) -> None:
    if not _is_admin(update):
        await _deny(update)
        return
    if not EXEC_ENABLED:
        await _send_or_edit(
            update,
            "Run command is disabled.\n"
            "Enable EXEC_ENABLED in .env and run install.sh --upgrade.",
            reply_markup=_exec_back_keyboard(server_id),
        )
        return
    if context is not None:
        _set_awaiting(context, "exec", server_id=server_id)
    label = _server_label(server_id)
    await _send_or_edit(
        update,
        f"Run command on {label}\n\n"
        "Send a shell command.\n"
        "Examples:\n"
        "  df -h\n"
        "  systemctl status nginx\n"
        "  docker ps",
        reply_markup=_exec_back_keyboard(server_id),
    )


async def _run_exec_for_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    server_id: str,
    command: str,
) -> None:
    if not _is_admin(update):
        _clear_awaiting(context)
        return
    label = _server_label(server_id)
    chat_id = _chat_id(update)
    logger.info("Admin exec on %s (chat %s): %s", label, chat_id, command)
    try:
        result = await _execute_on_server(server_id, command)
    except RemoteAgentError as exc:
        back_btn = InlineKeyboardButton(
            "◀️ Back to server", callback_data=_cb("pick", server_id)
        )
        await _send_or_edit(
            update,
            f"Could not run command on {label}: {exc}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⌨️ Try again",
                            callback_data=_cb("exec", "s", server_id),
                        )
                    ],
                    [back_btn],
                ]
            ),
        )
        return

    text = format_command_result(label=label, command=command, result=result)
    back_btn = InlineKeyboardButton(
        "◀️ Back to server", callback_data=_cb("pick", server_id)
    )
    await _send_or_edit(
        update,
        text,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⌨️ Run another",
                        callback_data=_cb("exec", "s", server_id),
                    )
                ],
                [back_btn],
            ]
        ),
        prefer_new=True,
    )


def _cb(*parts: str) -> str:
    return ":".join((CB, *parts))


def _parse_cb(data: str) -> list[str]:
    if not data.startswith(CB + ":"):
        return []
    return data.split(":")[1:]


def _back_button(*parts: str) -> InlineKeyboardButton:
    target = parts if parts else ("menu",)
    return InlineKeyboardButton("◀️ Back", callback_data=_cb(*target))


def _main_menu_keyboard(update: Update) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("📊 Status", callback_data=_cb("status")),
            InlineKeyboardButton("📋 All servers", callback_data=_cb("status", "all")),
        ],
        [
            InlineKeyboardButton("🖥 Servers", callback_data=_cb("servers")),
            InlineKeyboardButton("⚙️ Settings", callback_data=_cb("settings")),
        ],
        [InlineKeyboardButton("🆔 My ID", callback_data=_cb("id"))],
    ]
    if _is_admin(update):
        rows.insert(
            2,
            [InlineKeyboardButton("📦 Backup path", callback_data=_cb("bk"))],
        )
        rows.append(
            [
                InlineKeyboardButton("👤 Users", callback_data=_cb("admin", "users")),
                InlineKeyboardButton(
                    "🖥 Manage servers", callback_data=_cb("admin", "servers")
                ),
            ]
        )
    return InlineKeyboardMarkup(rows)


def _on_off(enabled: bool) -> str:
    return "ON ✅" if enabled else "OFF ❌"


def _format_server_alerts(server_id: str) -> str:
    cfg = config.get_server_alerts(server_id)
    label = _server_label(server_id)

    return (
        f"🔔 Alerts — {label}\n\n"
        f"Master switch: {_on_off(cfg['enabled'])}\n\n"
        f"CPU: {_on_off(cfg['cpu_enabled'])} — "
        f"{cfg['cpu_threshold_percent']:.0f}% for {cfg['cpu_sustained_checks']} checks\n"
        f"RAM: {_on_off(cfg['ram_enabled'])} — "
        f"{cfg['ram_threshold_percent']:.0f}%\n"
        f"Disk: {_on_off(cfg['disk_enabled'])} — "
        f"{cfg['disk_threshold_percent']:.0f}% used\n\n"
        "Toggle resources below, or tap one to set thresholds."
    )


def _format_global_alerts() -> str:
    data = config.data
    show_top = data.get("alert_show_top_processes", True)
    return (
        "⏱ Global monitoring\n\n"
        f"Check interval: {data['check_interval_seconds']}s\n"
        f"Alert cooldown: {data['alert_cooldown_seconds']}s\n"
        f"Local disk path: {data['disk_path']}\n"
        f"Show top apps in alerts: {_on_off(show_top)}\n\n"
        "These apply to all servers."
    )


def _format_alert_picker_text() -> str:
    return (
        "⚙️ Alert settings\n\n"
        "Pick a server to configure alerts separately.\n"
        "🔔 = alerts on  🔕 = alerts off\n"
        "[C/R/D] = CPU / RAM / Disk enabled\n\n"
        "Each server can have different resources and thresholds."
    )


async def _show_alert_picker(update: Update) -> None:
    await _send_or_edit(
        update,
        _format_alert_picker_text(),
        reply_markup=_alert_server_picker_keyboard(),
    )


def _alert_server_picker_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for sid in _monitor_target_ids():
        cfg = config.get_server_alerts(sid)
        icon = "🔔" if cfg["enabled"] else "🔕"
        tags = ""
        if cfg["cpu_enabled"]:
            tags += "C"
        if cfg["ram_enabled"]:
            tags += "R"
        if cfg["disk_enabled"]:
            tags += "D"
        tag_text = f" [{tags}]" if tags else " [—]"
        rows.append(
            [
                InlineKeyboardButton(
                    f"{icon} {_server_label(sid)}{tag_text}",
                    callback_data=_cb("al", "s", sid),
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("⏱ Global timing", callback_data=_cb("al", "g"))]
    )
    rows.append([_back_button()])
    return InlineKeyboardMarkup(rows)


def _server_alert_keyboard(server_id: str) -> InlineKeyboardMarkup:
    cfg = config.get_server_alerts(server_id)
    master_label = (
        "🔕 Disable all alerts" if cfg["enabled"] else "🔔 Enable all alerts"
    )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    master_label,
                    callback_data=_cb("al", "s", server_id, "m", "0" if cfg["enabled"] else "1"),
                )
            ],
            [
                InlineKeyboardButton(
                    f"CPU {_on_off(cfg['cpu_enabled'])}",
                    callback_data=_cb("al", "s", server_id, "cpu"),
                ),
                InlineKeyboardButton(
                    f"RAM {_on_off(cfg['ram_enabled'])}",
                    callback_data=_cb("al", "s", server_id, "ram"),
                ),
                InlineKeyboardButton(
                    f"Disk {_on_off(cfg['disk_enabled'])}",
                    callback_data=_cb("al", "s", server_id, "disk"),
                ),
            ],
            [
                InlineKeyboardButton(
                    "Toggle CPU",
                    callback_data=_cb("al", "s", server_id, "c", "t"),
                ),
                InlineKeyboardButton(
                    "Toggle RAM",
                    callback_data=_cb("al", "s", server_id, "r", "t"),
                ),
                InlineKeyboardButton(
                    "Toggle Disk",
                    callback_data=_cb("al", "s", server_id, "d", "t"),
                ),
            ],
            [
                InlineKeyboardButton(
                    "◀️ Servers", callback_data=_cb("al")
                ),
                _back_button(),
            ],
        ]
    )


def _cpu_alert_keyboard(server_id: str) -> InlineKeyboardMarkup:
    cfg = config.get_server_alerts(server_id)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"CPU alerts {_on_off(cfg['cpu_enabled'])}",
                    callback_data=_cb("al", "s", server_id, "c", "t"),
                )
            ],
            [
                InlineKeyboardButton("80%", callback_data=_cb("al", "s", server_id, "c", "p", "80")),
                InlineKeyboardButton("85%", callback_data=_cb("al", "s", server_id, "c", "p", "85")),
                InlineKeyboardButton("90%", callback_data=_cb("al", "s", server_id, "c", "p", "90")),
                InlineKeyboardButton("95%", callback_data=_cb("al", "s", server_id, "c", "p", "95")),
            ],
            [
                InlineKeyboardButton("Checks ×3", callback_data=_cb("al", "s", server_id, "c", "k", "3")),
                InlineKeyboardButton("Checks ×5", callback_data=_cb("al", "s", server_id, "c", "k", "5")),
                InlineKeyboardButton("Checks ×10", callback_data=_cb("al", "s", server_id, "c", "k", "10")),
            ],
            [
                InlineKeyboardButton(
                    "✏️ Custom %", callback_data=_cb("al", "s", server_id, "c", "u", "p")
                ),
                InlineKeyboardButton(
                    "✏️ Custom checks", callback_data=_cb("al", "s", server_id, "c", "u", "k")
                ),
            ],
            [_back_button("al", "s", server_id)],
        ]
    )


def _ram_alert_keyboard(server_id: str) -> InlineKeyboardMarkup:
    cfg = config.get_server_alerts(server_id)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"RAM alerts {_on_off(cfg['ram_enabled'])}",
                    callback_data=_cb("al", "s", server_id, "r", "t"),
                )
            ],
            [
                InlineKeyboardButton("80%", callback_data=_cb("al", "s", server_id, "r", "p", "80")),
                InlineKeyboardButton("90%", callback_data=_cb("al", "s", server_id, "r", "p", "90")),
                InlineKeyboardButton("95%", callback_data=_cb("al", "s", server_id, "r", "p", "95")),
            ],
            [
                InlineKeyboardButton(
                    "✏️ Custom %", callback_data=_cb("al", "s", server_id, "r", "u", "p")
                ),
            ],
            [_back_button("al", "s", server_id)],
        ]
    )


def _disk_alert_keyboard(server_id: str) -> InlineKeyboardMarkup:
    cfg = config.get_server_alerts(server_id)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"Disk alerts {_on_off(cfg['disk_enabled'])}",
                    callback_data=_cb("al", "s", server_id, "d", "t"),
                )
            ],
            [
                InlineKeyboardButton("80%", callback_data=_cb("al", "s", server_id, "d", "p", "80")),
                InlineKeyboardButton("90%", callback_data=_cb("al", "s", server_id, "d", "p", "90")),
                InlineKeyboardButton("95%", callback_data=_cb("al", "s", server_id, "d", "p", "95")),
            ],
            [
                InlineKeyboardButton(
                    "✏️ Custom %", callback_data=_cb("al", "s", server_id, "d", "u", "p")
                ),
            ],
            [_back_button("al", "s", server_id)],
        ]
    )


def _global_alert_keyboard() -> InlineKeyboardMarkup:
    show_top = config.data.get("alert_show_top_processes", True)
    top_label = (
        "📋 Top apps in alerts: ON ✅"
        if show_top
        else "📋 Top apps in alerts: OFF ❌"
    )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    top_label, callback_data=_cb("al", "g", "top", "t")
                )
            ],
            [
                InlineKeyboardButton("Every 30s", callback_data=_cb("al", "g", "i", "30")),
                InlineKeyboardButton("Every 60s", callback_data=_cb("al", "g", "i", "60")),
                InlineKeyboardButton("Every 120s", callback_data=_cb("al", "g", "i", "120")),
            ],
            [
                InlineKeyboardButton("CD 5m", callback_data=_cb("al", "g", "cd", "300")),
                InlineKeyboardButton("CD 10m", callback_data=_cb("al", "g", "cd", "600")),
                InlineKeyboardButton("CD 30m", callback_data=_cb("al", "g", "cd", "1800")),
            ],
            [
                InlineKeyboardButton(
                    "✏️ Custom interval", callback_data=_cb("al", "g", "u", "i")
                ),
                InlineKeyboardButton(
                    "✏️ Custom cooldown", callback_data=_cb("al", "g", "u", "cd")
                ),
            ],
            [_back_button("al")],
        ]
    )


async def _servers_keyboard(chat_id: int | None) -> InlineKeyboardMarkup:
    selected = _resolve_selection(chat_id)
    rows: list[list[InlineKeyboardButton]] = []
    if MONITOR_LOCAL:
        mark = "✓ " if selected == LOCAL_SERVER_ID else ""
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark}{_local_display_name()} (local)",
                    callback_data=_cb("pick", LOCAL_SERVER_ID),
                )
            ]
        )
    for entry in servers.ready_servers:
        mark = "✓ " if selected == entry["id"] else ""
        online = await ping_agent(entry["url"], entry["token"])
        dot = "🟢" if online else "🔴"
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark}{dot} {entry['name']}",
                    callback_data=_cb("pick", entry["id"]),
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("📋 All servers", callback_data=_cb("status", "all"))]
    )
    rows.append([_back_button()])
    return InlineKeyboardMarkup(rows)


def _server_actions_keyboard(server_id: str, update: Update) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                "📊 Status", callback_data=_cb("status", server_id)
            )
        ],
    ]
    if _is_admin(update) and EXEC_ENABLED:
        rows.append(
            [
                InlineKeyboardButton(
                    "⌨️ Run command",
                    callback_data=_cb("exec", "s", server_id),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("🖥 Servers", callback_data=_cb("servers")),
            _back_button(),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _admin_servers_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for entry in servers.servers:
        rows.append(
            [
                InlineKeyboardButton(
                    f"🗑 {entry['name']}",
                    callback_data=_cb("admin", "rmsrv", entry["id"]),
                ),
                InlineKeyboardButton(
                    "✏️ Rename",
                    callback_data=_cb("admin", "rename", entry["id"]),
                ),
            ]
        )
    rows.append(
        [InlineKeyboardButton("➕ Add server", callback_data=_cb("admin", "addsrv"))]
    )
    rows.append([_back_button()])
    return InlineKeyboardMarkup(rows)


def _admin_users_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for chat_id in sorted(allowed_users.chat_ids):
        rows.append(
            [
                InlineKeyboardButton(
                    f"🗑 {chat_id}",
                    callback_data=_cb("admin", "rmuser", str(chat_id)),
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("➕ Add user", callback_data=_cb("admin", "adduser"))]
    )
    rows.append([_back_button()])
    return InlineKeyboardMarkup(rows)


def _status_actions_keyboard(
    server_id: str | None, update: Update | None = None
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔄 Refresh", callback_data=_cb("status", "refresh"))],
        [
            InlineKeyboardButton("🖥 Servers", callback_data=_cb("servers")),
            _back_button(),
        ],
    ]
    if server_id:
        rows[1].insert(
            0,
            InlineKeyboardButton(
                "◀️ Server menu",
                callback_data=_cb("pick", server_id),
            ),
        )
        rows[0].insert(
            0,
            InlineKeyboardButton(
                "📊 This server", callback_data=_cb("status", server_id)
            ),
        )
    return InlineKeyboardMarkup(rows)


def _format_id_text(update: Update) -> str:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None:
        return ""
    lines = [f"Chat ID: `{chat.id}`"]
    if user is not None:
        lines.append(f"User ID: `{user.id}`")
    if chat.type != "private" and user is not None:
        lines.append(
            "\nIn groups, authorize this chat ID. In DMs, either chat or user ID works."
        )
    if _authorized(update):
        lines.append("\nYou are authorized.")
    elif _is_admin(update):
        lines.append("\nYou are an admin.")
    else:
        lines.append(
            "\nYou are not authorized yet. Send this ID to an admin to get access."
        )
    return "\n".join(lines)


async def _send_or_edit(
    update: Update,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
    prefer_new: bool = False,
) -> None:
    if update.callback_query and not prefer_new:
        query = update.callback_query
        await query.answer()
        try:
            await query.edit_message_text(
                text, reply_markup=reply_markup, parse_mode=parse_mode
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                await query.message.reply_text(
                    text, reply_markup=reply_markup, parse_mode=parse_mode
                )
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(
            text, reply_markup=reply_markup, parse_mode=parse_mode
        )
    elif update.message:
        await update.message.reply_text(
            text, reply_markup=reply_markup, parse_mode=parse_mode
        )


async def _deny(update: Update) -> None:
    text = (
        "Unauthorized. Tap My ID below, then ask an admin to add you."
        if update.callback_query
        else "Unauthorized. Send /start and tap My ID, then ask an admin to add you."
    )
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🆔 My ID", callback_data=_cb("id"))]]
    )
    await _send_or_edit(update, text, reply_markup=markup)


async def _show_menu(update: Update, *, text: str | None = None) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    message = text or "Server resource monitor\n\nChoose an action:"
    await _send_or_edit(update, message, reply_markup=_main_menu_keyboard(update))


async def _build_status_text(server_id: str) -> str:
    snapshot = await _fetch_status(server_id)
    label = _server_label(server_id)
    display_name = (
        None if server_id == LOCAL_SERVER_ID and not LOCAL_SERVER_NAME else label
    )
    return format_status(snapshot, display_name=display_name)


async def _show_status(update: Update, server_id: str | None = None) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    if server_id is None:
        server_id = _resolve_selection(_chat_id(update))
    if server_id is None:
        await _send_or_edit(
            update,
            "Multiple servers available. Pick one from the list.",
            reply_markup=await _servers_keyboard(_chat_id(update)),
        )
        return
    try:
        text = await _build_status_text(server_id)
    except RemoteAgentError as exc:
        await _send_or_edit(
            update,
            f"Could not reach {_server_label(server_id)}: {exc}",
            reply_markup=await _servers_keyboard(_chat_id(update)),
        )
        return
    await _send_or_edit(
        update,
        text,
        reply_markup=_status_actions_keyboard(server_id, update),
        prefer_new=True,
    )


async def _show_status_all(update: Update) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    targets = _monitor_target_ids()
    if not targets:
        await _show_menu(update, text="No servers configured.")
        return
    lines = ["All servers\n"]
    for sid in targets:
        label = _server_label(sid)
        try:
            snapshot = await _fetch_status(sid)
            lines.append(
                f"{label}: CPU {snapshot.cpu_percent_avg:.0f}%, "
                f"RAM {snapshot.ram_percent:.0f}%, "
                f"Disk {snapshot.disk_percent:.0f}%"
            )
        except RemoteAgentError:
            lines.append(f"{label}: unreachable")
    await _send_or_edit(
        update,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔄 Refresh", callback_data=_cb("status", "all")
                    )
                ],
                [_back_button()],
            ]
        ),
    )


async def _show_servers(update: Update) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    chat_id = _chat_id(update)
    selected = _resolve_selection(chat_id)
    lines = ["Registered servers\nTap a server to open its menu.\n"]
    if MONITOR_LOCAL:
        marker = " ← selected" if selected == LOCAL_SERVER_ID else ""
        lines.append(f"  local — {_local_display_name()}{marker}")
    for entry in servers.ready_servers:
        marker = " ← selected" if selected == entry["id"] else ""
        online = await ping_agent(entry["url"], entry["token"])
        state = "online" if online else "offline"
        lines.append(f"  {entry['name']} — {state}{marker}")
    if not servers.servers and not MONITOR_LOCAL:
        lines.append("  (none)")
    await _send_or_edit(
        update, "\n".join(lines), reply_markup=await _servers_keyboard(chat_id)
    )


async def _pick_server(update: Update, server_id: str) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    chat_id = _chat_id(update)
    if chat_id is None:
        return
    if server_id != LOCAL_SERVER_ID and servers.get(server_id) is None:
        await _show_servers(update)
        return
    servers.set_selection(chat_id, server_id)
    await _show_server_actions(update, server_id)


async def _show_settings(update: Update) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    targets = _monitor_target_ids()
    if len(targets) == 1:
        await _show_server_alerts(update, targets[0])
        return
    await _show_alert_picker(update)


async def _show_server_alerts(update: Update, server_id: str) -> None:
    if server_id != LOCAL_SERVER_ID and servers.get(server_id) is None:
        await _show_settings(update)
        return
    await _send_or_edit(
        update,
        _format_server_alerts(server_id),
        reply_markup=_server_alert_keyboard(server_id),
    )


async def _show_cpu_alerts(update: Update, server_id: str) -> None:
    cfg = config.get_server_alerts(server_id)
    text = (
        f"CPU alerts — {_server_label(server_id)}\n\n"
        f"Status: {_on_off(cfg['cpu_enabled'])}\n"
        f"Threshold: {cfg['cpu_threshold_percent']:.0f}%\n"
        f"Sustained checks: {cfg['cpu_sustained_checks']}\n\n"
        "Triggers when average CPU stays at/above the threshold "
        "for the chosen number of checks.\n"
        "Pick a preset or send a custom value."
    )
    await _send_or_edit(
        update, text, reply_markup=_cpu_alert_keyboard(server_id)
    )


async def _show_ram_alerts(update: Update, server_id: str) -> None:
    cfg = config.get_server_alerts(server_id)
    text = (
        f"RAM alerts — {_server_label(server_id)}\n\n"
        f"Status: {_on_off(cfg['ram_enabled'])}\n"
        f"Threshold: {cfg['ram_threshold_percent']:.0f}%"
    )
    await _send_or_edit(
        update, text, reply_markup=_ram_alert_keyboard(server_id)
    )


async def _show_disk_alerts(update: Update, server_id: str) -> None:
    cfg = config.get_server_alerts(server_id)
    text = (
        f"Disk alerts — {_server_label(server_id)}\n\n"
        f"Status: {_on_off(cfg['disk_enabled'])}\n"
        f"Threshold: {cfg['disk_threshold_percent']:.0f}% used\n"
    )
    await _send_or_edit(
        update, text, reply_markup=_disk_alert_keyboard(server_id)
    )


async def _show_global_alerts(update: Update) -> None:
    await _send_or_edit(
        update,
        _format_global_alerts(),
        reply_markup=_global_alert_keyboard(),
    )


def _format_backup_text() -> str:
    b = config.get_backup()
    path = b.get("path") or "(not set)"
    interval = int(b.get("interval_minutes") or 60)
    limit_mb = TELEGRAM_UPLOAD_LIMIT_BYTES / (1024 * 1024)
    last = ""
    if _last_backup_at > 0:
        last = datetime.fromtimestamp(_last_backup_at).strftime("%Y-%m-%d %H:%M:%S")
    else:
        last = "never"
    return (
        "📦 Backup path\n\n"
        "Zips a local path on this bot host and uploads it to Telegram.\n"
        f"Upload limit: {limit_mb:.0f} MB (Telegram Bot API).\n\n"
        f"Path: {path}\n"
        f"Repeat every: {interval} minute(s)\n"
        f"Schedule: {_on_off(bool(b.get('enabled')))}\n"
        f"Last backup: {last}\n\n"
        "Set a path, optional repeat interval, then Run now or enable the schedule."
    )


def _backup_keyboard() -> InlineKeyboardMarkup:
    b = config.get_backup()
    toggle = "⏹ Stop schedule" if b.get("enabled") else "▶️ Start schedule"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📁 Set path", callback_data=_cb("bk", "path")),
                InlineKeyboardButton(
                    "⏱ Interval (min)", callback_data=_cb("bk", "int")
                ),
            ],
            [
                InlineKeyboardButton("5 min", callback_data=_cb("bk", "i", "5")),
                InlineKeyboardButton("30 min", callback_data=_cb("bk", "i", "30")),
                InlineKeyboardButton("60 min", callback_data=_cb("bk", "i", "60")),
                InlineKeyboardButton("120 min", callback_data=_cb("bk", "i", "120")),
            ],
            [
                InlineKeyboardButton("📤 Run now", callback_data=_cb("bk", "run")),
                InlineKeyboardButton(toggle, callback_data=_cb("bk", "tog")),
            ],
            [
                InlineKeyboardButton(
                    "📋 Linux logs", callback_data=_cb("bk", "logs")
                )
            ],
            [_back_button()],
        ]
    )


def _format_linux_logs_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    found = _ensure_linux_log_selection(context)
    selected = _linux_log_selection(context)
    denied = sum(
        1 for _p, status, _d in list_important_linux_logs() if status == "denied"
    )
    limit_mb = TELEGRAM_UPLOAD_LIMIT_BYTES / (1024 * 1024)
    lines = [
        "📋 Important Linux logs\n",
        f"Readable: {len(found)}  |  selected: {len(selected)}  |  no access: {denied}",
        f"Zip upload limit: {limit_mb:.0f} MB\n",
        "Tap checkboxes below to choose which logs to upload.",
    ]
    if not found:
        lines.append(
            "\nNo readable log files found. Run the bot as root (or a user "
            "that can read /var/log)."
        )
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3970] + "\n…(truncated)"
    return text


def _linux_log_short_name(path: str) -> str:
    p = Path(path)
    if p.parent == Path("/var/log"):
        return p.name
    return f"{p.parent.name}/{p.name}"


def _linux_log_button_label(path: str, selected: bool, detail: str) -> str:
    mark = "✅" if selected else "⬜"
    short = _linux_log_short_name(path)
    label = f"{mark} {short}"
    if detail:
        candidate = f"{label} ({detail})"
        if len(candidate) <= 64:
            return candidate
    return label[:64]


def _linux_logs_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    found = _ensure_linux_log_selection(context)
    selected = _linux_log_selection(context)
    path_to_idx = {path: idx for idx, path in enumerate(IMPORTANT_LINUX_LOGS)}
    rows: list[list[InlineKeyboardButton]] = []
    for path, detail in found:
        idx = path_to_idx[path]
        rows.append(
            [
                InlineKeyboardButton(
                    _linux_log_button_label(path, path in selected, detail),
                    callback_data=_cb("bk", "logs", "t", str(idx)),
                )
            ]
        )
    if found:
        rows.append(
            [
                InlineKeyboardButton(
                    "✅ Select all", callback_data=_cb("bk", "logs", "all")
                ),
                InlineKeyboardButton(
                    "⬜ Clear", callback_data=_cb("bk", "logs", "none")
                ),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    f"📤 Upload selected ({len(selected)})",
                    callback_data=_cb("bk", "logs", "up"),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=_cb("bk", "logs")),
            InlineKeyboardButton("◀️ Back", callback_data=_cb("bk")),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _linux_log_selection(context: ContextTypes.DEFAULT_TYPE) -> set[str]:
    sel = context.user_data.get("linux_log_selection")
    if not isinstance(sel, set):
        sel = set()
        context.user_data["linux_log_selection"] = sel
    return sel


def _ensure_linux_log_selection(
    context: ContextTypes.DEFAULT_TYPE,
) -> list[tuple[str, str]]:
    """Return [(path, size_detail), ...] for readable logs; sync selection."""
    found = [
        (path, detail)
        for path, status, detail in list_important_linux_logs()
        if status == "found"
    ]
    found_paths = {path for path, _detail in found}
    if "linux_log_selection" not in context.user_data:
        context.user_data["linux_log_selection"] = set(found_paths)
    else:
        _linux_log_selection(context).intersection_update(found_paths)
    return found


async def _show_linux_logs(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not _is_admin(update):
        await _deny(update)
        return
    await _send_or_edit(
        update,
        _format_linux_logs_text(context),
        reply_markup=_linux_logs_keyboard(context),
    )


async def _show_backup(update: Update) -> None:
    if not _is_admin(update):
        await _deny(update)
        return
    await _send_or_edit(
        update,
        _format_backup_text(),
        reply_markup=_backup_keyboard(),
    )


async def _send_zip_document(
    bot,
    chat_id: int,
    *,
    zip_factory,
    filename_stem: str,
    caption: str | None = None,
    bump_schedule: bool = False,
) -> list[str]:
    """Build a zip via *zip_factory* and upload it. Returns included paths if any."""
    global _backup_running, _last_backup_at
    if _backup_running:
        raise BackupError("A backup is already running. Try again shortly.")
    _backup_running = True
    zip_path: Path | None = None
    included: list[str] = []
    try:
        result = await asyncio.to_thread(zip_factory)
        if isinstance(result, tuple):
            zip_path, included = result
        else:
            zip_path = result
        size = zip_path.stat().st_size
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{filename_stem}_{stamp}.zip"
        size_line = f"Size: {format_size(size)}"
        if caption:
            text = f"{caption}\n{size_line}"
        else:
            text = f"📦 Backup\n{size_line}"
        if included:
            preview = ", ".join(Path(p).name for p in included[:8])
            more = f" (+{len(included) - 8} more)" if len(included) > 8 else ""
            text = f"{text}\nFiles: {len(included)} ({preview}{more})"
        # Telegram caption max 1024
        if len(text) > 1000:
            text = text[:997] + "..."
        with zip_path.open("rb") as handle:
            await bot.send_document(
                chat_id=chat_id,
                document=InputFile(handle, filename=filename),
                caption=text,
            )
        if bump_schedule:
            _last_backup_at = time.time()
        return included
    finally:
        _backup_running = False
        if zip_path is not None:
            zip_path.unlink(missing_ok=True)


async def _send_backup_document(
    bot,
    chat_id: int,
    source_path: str,
    *,
    caption: str | None = None,
) -> None:
    name = Path(source_path).expanduser().name or "backup"
    await _send_zip_document(
        bot,
        chat_id,
        zip_factory=lambda: create_backup_zip(source_path),
        filename_stem=name,
        caption=caption or f"📦 Backup of {source_path}",
        bump_schedule=True,
    )


async def _handle_backup_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]
) -> None:
    if not _is_admin(update):
        await _deny(update)
        return

    if not parts:
        _clear_awaiting(context)
        await _show_backup(update)
        return

    action = parts[0]

    if action == "path":
        _set_awaiting(context, "backup_path")
        await _send_or_edit(
            update,
            "Send the absolute path to back up (file or folder).\n"
            f"Zip must stay under {TELEGRAM_UPLOAD_LIMIT_BYTES // (1024 * 1024)} MB.",
            reply_markup=InlineKeyboardMarkup([[_back_button("bk")]]),
        )
        return

    if action == "int":
        _set_awaiting(context, "backup_interval")
        await _send_or_edit(
            update,
            "Send repeat interval in minutes (minimum 1).\n"
            "Example: 30",
            reply_markup=InlineKeyboardMarkup([[_back_button("bk")]]),
        )
        return

    if action == "i" and len(parts) > 1:
        try:
            minutes = int(parts[1])
        except ValueError:
            await _show_backup(update)
            return
        if minutes < 1:
            minutes = 1
        config.update_backup(interval_minutes=minutes)
        await _show_backup(update)
        return

    if action == "tog":
        b = config.get_backup()
        if not b.get("enabled"):
            if not (b.get("path") or "").strip():
                query = update.callback_query
                if query:
                    await query.answer("Set a path first", show_alert=True)
                return
            chat_id = _chat_id(update)
            config.update_backup(enabled=True, notify_chat_id=chat_id)
        else:
            config.update_backup(enabled=False)
        await _show_backup(update)
        return

    if action == "run":
        b = config.get_backup()
        path = (b.get("path") or "").strip()
        chat_id = _chat_id(update)
        query = update.callback_query
        if not path:
            if query:
                await query.answer("Set a path first", show_alert=True)
            return
        if chat_id is None:
            return
        if query:
            await query.answer("Creating zip…")
        await _send_or_edit(
            update,
            f"📦 Creating backup of:\n{path}\n\nPlease wait…",
            reply_markup=_backup_keyboard(),
        )
        try:
            await _send_backup_document(
                context.application.bot,
                chat_id,
                path,
            )
            await _show_backup(update)
        except BackupError as exc:
            await _send_or_edit(
                update,
                f"❌ Backup failed:\n{exc}",
                reply_markup=_backup_keyboard(),
            )
        except Exception as exc:
            logger.exception("Backup upload failed")
            await _send_or_edit(
                update,
                f"❌ Backup upload failed:\n{exc}",
                reply_markup=_backup_keyboard(),
            )
        return

    if action == "logs":
        sub = parts[1] if len(parts) > 1 else ""

        if sub == "t" and len(parts) > 2:
            try:
                idx = int(parts[2])
            except ValueError:
                await _show_linux_logs(update, context)
                return
            if 0 <= idx < len(IMPORTANT_LINUX_LOGS):
                path = IMPORTANT_LINUX_LOGS[idx]
                status, _detail = classify_log_path(path)
                if status == "found":
                    sel = _linux_log_selection(context)
                    if path in sel:
                        sel.discard(path)
                    else:
                        sel.add(path)
            await _show_linux_logs(update, context)
            return

        if sub == "all":
            found = _ensure_linux_log_selection(context)
            _linux_log_selection(context).update(path for path, _d in found)
            await _show_linux_logs(update, context)
            return

        if sub == "none":
            _ensure_linux_log_selection(context)
            _linux_log_selection(context).clear()
            await _show_linux_logs(update, context)
            return

        if sub == "up":
            chat_id = _chat_id(update)
            query = update.callback_query
            if chat_id is None:
                return
            _ensure_linux_log_selection(context)
            selected = sorted(_linux_log_selection(context))
            if not selected:
                if query:
                    await query.answer(
                        "Select at least one log first.", show_alert=True
                    )
                return
            if query:
                await query.answer("Collecting logs…")
            await _send_or_edit(
                update,
                f"📋 Zipping {len(selected)} selected log(s)…\nPlease wait…",
                reply_markup=_linux_logs_keyboard(context),
            )
            try:
                await _send_zip_document(
                    context.application.bot,
                    chat_id,
                    zip_factory=lambda: create_paths_zip(
                        selected, prefix="linux_logs_"
                    ),
                    filename_stem="linux_logs",
                    caption="📋 Selected Linux logs backup",
                )
                await _show_linux_logs(update, context)
            except BackupError as exc:
                await _send_or_edit(
                    update,
                    f"❌ Logs upload failed:\n{exc}",
                    reply_markup=_linux_logs_keyboard(context),
                )
            except Exception as exc:
                logger.exception("Linux logs upload failed")
                await _send_or_edit(
                    update,
                    f"❌ Logs upload failed:\n{exc}",
                    reply_markup=_linux_logs_keyboard(context),
                )
            return

        # Fresh open / refresh — reselect all currently readable logs
        context.user_data.pop("linux_log_selection", None)
        await _show_linux_logs(update, context)
        return

    await _show_backup(update)


async def _handle_alert_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]
) -> None:
    if not _authorized(update):
        await _deny(update)
        return

    if not parts:
        # Always show the server list — used by the "◀️ Servers" button.
        # Do not call _show_settings() here: with one server that shortcuts
        # back to the same alert screen and looks broken.
        await _show_alert_picker(update)
        return

    if parts[0] == "g":
        await _handle_global_alert_callback(update, context, parts[1:])
        return

    if parts[0] != "s" or len(parts) < 2:
        await _show_alert_picker(update)
        return

    server_id = parts[1]
    rest = parts[2:]

    if not rest:
        await _show_server_alerts(update, server_id)
        return

    action = rest[0]

    if action == "m" and len(rest) > 1:
        config.update_server_alerts(server_id, enabled=(rest[1] == "1"))
        await _show_server_alerts(update, server_id)
        return

    if action in {"cpu", "ram", "disk"} and len(rest) == 1:
        if action == "cpu":
            await _show_cpu_alerts(update, server_id)
        elif action == "ram":
            await _show_ram_alerts(update, server_id)
        else:
            await _show_disk_alerts(update, server_id)
        return

    if action == "c":
        await _apply_cpu_alert_change(update, context, server_id, rest[1:])
        return
    if action == "r":
        await _apply_ram_alert_change(update, context, server_id, rest[1:])
        return
    if action == "d":
        await _apply_disk_alert_change(update, context, server_id, rest[1:])
        return

    await _show_server_alerts(update, server_id)


async def _handle_global_alert_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]
) -> None:
    if not parts:
        await _show_global_alerts(update)
        return

    if parts[0] == "i" and len(parts) > 1:
        config.update_global(check_interval_seconds=int(parts[1]))
        await _show_global_alerts(update)
        return

    if parts[0] == "cd" and len(parts) > 1:
        config.update_global(alert_cooldown_seconds=int(parts[1]))
        await _show_global_alerts(update)
        return

    if parts[0] == "top" and len(parts) > 1 and parts[1] == "t":
        current = config.data.get("alert_show_top_processes", True)
        config.update_global(alert_show_top_processes=not current)
        await _show_global_alerts(update)
        return

    if parts[0] == "u" and len(parts) > 1:
        field = parts[1]
        prompts = {
            "i": ("Send check interval in seconds (min 10):", "al_cfg_interval"),
            "cd": ("Send alert cooldown in seconds (min 60):", "al_cfg_cooldown"),
        }
        if field in prompts:
            prompt, action = prompts[field]
            _set_awaiting(context, action)
            await _send_or_edit(
                update,
                prompt,
                reply_markup=InlineKeyboardMarkup([[_back_button("al", "g")]]),
            )
        return

    await _show_global_alerts(update)


async def _apply_cpu_alert_change(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    server_id: str,
    parts: list[str],
) -> None:
    if not parts:
        await _show_cpu_alerts(update, server_id)
        return

    if parts[0] == "t":
        cfg = config.get_server_alerts(server_id)
        config.update_server_alerts(server_id, cpu_enabled=not cfg["cpu_enabled"])
        await _show_cpu_alerts(update, server_id)
        return

    if parts[0] == "p" and len(parts) > 1:
        config.update_server_alerts(
            server_id, cpu_enabled=True, cpu_threshold_percent=float(parts[1])
        )
        await _show_cpu_alerts(update, server_id)
        return

    if parts[0] == "k" and len(parts) > 1:
        config.update_server_alerts(
            server_id, cpu_enabled=True, cpu_sustained_checks=int(parts[1])
        )
        await _show_cpu_alerts(update, server_id)
        return

    if parts[0] == "u" and len(parts) > 1:
        kind = parts[1]
        if kind == "p":
            _set_awaiting(context, "al_cfg_cpu_pct", server_id=server_id)
            await _send_or_edit(
                update,
                f"Send CPU threshold % for {_server_label(server_id)} (1–100):",
                reply_markup=InlineKeyboardMarkup(
                    [[_back_button("al", "s", server_id, "cpu")]]
                ),
            )
        elif kind == "k":
            _set_awaiting(context, "al_cfg_cpu_checks", server_id=server_id)
            await _send_or_edit(
                update,
                f"Send sustained check count for {_server_label(server_id)} (min 1):",
                reply_markup=InlineKeyboardMarkup(
                    [[_back_button("al", "s", server_id, "cpu")]]
                ),
            )
        return

    await _show_cpu_alerts(update, server_id)


async def _apply_ram_alert_change(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    server_id: str,
    parts: list[str],
) -> None:
    if not parts:
        await _show_ram_alerts(update, server_id)
        return

    if parts[0] == "t":
        cfg = config.get_server_alerts(server_id)
        config.update_server_alerts(server_id, ram_enabled=not cfg["ram_enabled"])
        await _show_ram_alerts(update, server_id)
        return

    if parts[0] == "p" and len(parts) > 1:
        config.update_server_alerts(
            server_id,
            ram_enabled=True,
            ram_threshold_percent=float(parts[1]),
        )
        await _show_ram_alerts(update, server_id)
        return

    if parts[0] == "u" and len(parts) > 1:
        kind = parts[1]
        if kind == "p":
            _set_awaiting(context, "al_cfg_ram_pct", server_id=server_id)
            await _send_or_edit(
                update,
                f"Send RAM threshold % for {_server_label(server_id)} (1–100):",
                reply_markup=InlineKeyboardMarkup(
                    [[_back_button("al", "s", server_id, "ram")]]
                ),
            )
        return

    await _show_ram_alerts(update, server_id)


async def _apply_disk_alert_change(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    server_id: str,
    parts: list[str],
) -> None:
    if not parts:
        await _show_disk_alerts(update, server_id)
        return

    if parts[0] == "t":
        cfg = config.get_server_alerts(server_id)
        config.update_server_alerts(server_id, disk_enabled=not cfg["disk_enabled"])
        await _show_disk_alerts(update, server_id)
        return

    if parts[0] == "p" and len(parts) > 1:
        config.update_server_alerts(
            server_id,
            disk_enabled=True,
            disk_threshold_percent=float(parts[1]),
        )
        await _show_disk_alerts(update, server_id)
        return

    if parts[0] == "u" and len(parts) > 1 and parts[1] == "p":
        _set_awaiting(context, "al_cfg_disk_pct", server_id=server_id)
        await _send_or_edit(
            update,
            f"Send disk threshold % for {_server_label(server_id)} (1–100):",
            reply_markup=InlineKeyboardMarkup(
                [[_back_button("al", "s", server_id, "disk")]]
            ),
        )
        return

    await _show_disk_alerts(update, server_id)


async def _show_admin_users(update: Update) -> None:
    if not _is_admin(update):
        await _deny(update)
        return
    lines = ["Allowed users\n"]
    lines.append("Admins (from .env):")
    for cid in sorted(ADMIN_CHAT_IDS):
        lines.append(f"  {cid}")
    added = sorted(allowed_users.chat_ids)
    lines.append("\nAdded via bot (tap to remove):")
    if added:
        for cid in added:
            lines.append(f"  {cid}")
    else:
        lines.append("  (none)")
    await _send_or_edit(
        update, "\n".join(lines), reply_markup=_admin_users_keyboard()
    )


async def _show_admin_servers(update: Update) -> None:
    if not _is_admin(update):
        await _deny(update)
        return
    lines = ["Manage remote servers\n"]
    if servers.servers:
        for entry in servers.servers:
            if ServersStore.is_ready(entry):
                lines.append(f"  • {entry['name']} — {entry['url']}")
            else:
                lines.append(
                    f"  • {entry['name']} — (missing URL — remove and re-add)"
                )
    else:
        lines.append("  (none)")
    lines.append("\nAdd: name | url | token (token optional)")
    if any(not ServersStore.is_ready(s) for s in servers.servers):
        lines.append(
            "\n⚠ Some entries have no URL (from an older install). "
            "Remove them and add again with the agent URL."
        )
    await _send_or_edit(
        update, "\n".join(lines), reply_markup=_admin_servers_keyboard()
    )


def _set_awaiting(context: ContextTypes.DEFAULT_TYPE, action: str, **extra) -> None:
    context.user_data["awaiting"] = action
    context.user_data["awaiting_extra"] = extra


def _clear_awaiting(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting", None)
    context.user_data.pop("awaiting_extra", None)


async def _handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    action = context.user_data.get("awaiting")
    if not action:
        return

    text = update.message.text.strip()
    extra = context.user_data.get("awaiting_extra", {})

    if action == "adduser":
        if not _is_admin(update):
            _clear_awaiting(context)
            return
        try:
            user_id = int(text)
        except ValueError:
            await update.message.reply_text(
                "Chat ID must be a number. Try again or tap Back in the menu.",
                reply_markup=InlineKeyboardMarkup([[_back_button()]]),
            )
            return
        _clear_awaiting(context)
        if user_id in ADMIN_CHAT_IDS:
            await update.message.reply_text(f"{user_id} is already an admin.")
        elif allowed_users.add(user_id):
            await update.message.reply_text(f"Added user {user_id}.")
        else:
            await update.message.reply_text(f"{user_id} was already authorized.")
        await _show_menu(update)
        return

    if action == "addserver":
        if not _is_admin(update):
            _clear_awaiting(context)
            return
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 2:
            await update.message.reply_text(
                "Use format: name | url | token\nToken is optional."
            )
            return
        name, url = parts[0], parts[1]
        token = parts[2] if len(parts) > 2 else ServersStore.generate_token()
        _clear_awaiting(context)
        try:
            entry = servers.add(name, url, token)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        online = await ping_agent(entry["url"], entry["token"])
        status_text = (
            "reachable" if online else "not reachable (check URL, firewall, agent)"
        )
        await update.message.reply_text(
            f"Added server {entry['name']}\n"
            f"URL: {entry['url']}\n"
            f"Token: {entry['token']}\n"
            f"Agent: {status_text}"
        )
        await _show_admin_servers(update)
        return

    if action == "exec":
        if not _is_admin(update):
            _clear_awaiting(context)
            return
        server_id = extra.get("server_id")
        if not server_id:
            _clear_awaiting(context)
            await update.message.reply_text("No server selected.")
            return
        if not text:
            await update.message.reply_text("Send a non-empty command.")
            return
        _clear_awaiting(context)
        await _run_exec_for_admin(update, context, server_id, text)
        return

    if action == "rename":
        if not _is_admin(update):
            _clear_awaiting(context)
            return
        server_id = extra.get("server_id")
        _clear_awaiting(context)
        if not server_id:
            return
        old = _server_label(server_id)
        try:
            if servers.rename(old, text):
                await update.message.reply_text(f"Renamed {old} → {text}.")
            else:
                await update.message.reply_text("Server not found.")
        except ValueError as exc:
            await update.message.reply_text(str(exc))
        await _show_admin_servers(update)
        return

    # alert custom values
    server_id = extra.get("server_id")

    if action == "al_cfg_cpu_pct" and server_id:
        try:
            value = float(text)
        except ValueError:
            await update.message.reply_text("Enter a number between 1 and 100.")
            return
        if not 1 <= value <= 100:
            await update.message.reply_text("Percent must be between 1 and 100.")
            return
        _clear_awaiting(context)
        config.update_server_alerts(
            server_id, cpu_enabled=True, cpu_threshold_percent=value
        )
        await update.message.reply_text(f"CPU threshold set to {value:.0f}%.")
        await _show_cpu_alerts(update, server_id)
        return

    if action == "al_cfg_cpu_checks" and server_id:
        try:
            value = int(text)
        except ValueError:
            await update.message.reply_text("Enter a whole number.")
            return
        if value < 1:
            await update.message.reply_text("Must be at least 1.")
            return
        _clear_awaiting(context)
        config.update_server_alerts(
            server_id, cpu_enabled=True, cpu_sustained_checks=value
        )
        await update.message.reply_text(f"CPU sustained checks set to {value}.")
        await _show_cpu_alerts(update, server_id)
        return

    if action == "al_cfg_ram_pct" and server_id:
        try:
            value = float(text)
        except ValueError:
            await update.message.reply_text("Enter a number between 1 and 100.")
            return
        if not 1 <= value <= 100:
            await update.message.reply_text("Percent must be between 1 and 100.")
            return
        _clear_awaiting(context)
        config.update_server_alerts(
            server_id, ram_enabled=True, ram_threshold_percent=value
        )
        await update.message.reply_text(f"RAM threshold set to {value:.0f}%.")
        await _show_ram_alerts(update, server_id)
        return

    if action == "al_cfg_disk_pct" and server_id:
        try:
            value = float(text)
        except ValueError:
            await update.message.reply_text("Enter a number between 1 and 100.")
            return
        if not 1 <= value <= 100:
            await update.message.reply_text("Percent must be between 1 and 100.")
            return
        _clear_awaiting(context)
        config.update_server_alerts(
            server_id, disk_enabled=True, disk_threshold_percent=value
        )
        await update.message.reply_text(f"Disk threshold set to {value:.0f}%.")
        await _show_disk_alerts(update, server_id)
        return

    if action == "al_cfg_interval":
        try:
            value = int(text)
        except ValueError:
            await update.message.reply_text("Enter seconds as a whole number.")
            return
        if value < 10:
            await update.message.reply_text("Interval must be at least 10 seconds.")
            return
        _clear_awaiting(context)
        config.update_global(check_interval_seconds=value)
        await update.message.reply_text(f"Check interval set to {value}s.")
        await _show_global_alerts(update)
        return

    if action == "al_cfg_cooldown":
        try:
            value = int(text)
        except ValueError:
            await update.message.reply_text("Enter seconds as a whole number.")
            return
        if value < 60:
            await update.message.reply_text("Cooldown must be at least 60 seconds.")
            return
        _clear_awaiting(context)
        config.update_global(alert_cooldown_seconds=value)
        await update.message.reply_text(f"Alert cooldown set to {value}s.")
        await _show_global_alerts(update)
        return

    if action == "backup_path":
        if not _is_admin(update):
            _clear_awaiting(context)
            return
        candidate = Path(text).expanduser()
        if not candidate.exists():
            await update.message.reply_text(
                f"Path not found: {text}\nSend an existing file or folder path."
            )
            return
        if not (candidate.is_file() or candidate.is_dir()):
            await update.message.reply_text("Path must be a file or directory.")
            return
        _clear_awaiting(context)
        resolved = str(candidate.resolve())
        chat_id = _chat_id(update)
        config.update_backup(path=resolved, notify_chat_id=chat_id)
        await update.message.reply_text(f"Backup path set to:\n{resolved}")
        await _show_backup(update)
        return

    if action == "backup_interval":
        if not _is_admin(update):
            _clear_awaiting(context)
            return
        try:
            minutes = int(text)
        except ValueError:
            await update.message.reply_text("Enter minutes as a whole number.")
            return
        if minutes < 1:
            await update.message.reply_text("Interval must be at least 1 minute.")
            return
        _clear_awaiting(context)
        config.update_backup(interval_minutes=minutes)
        await update.message.reply_text(f"Backup interval set to {minutes} minute(s).")
        await _show_backup(update)
        return


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    parts = _parse_cb(query.data)
    if not parts:
        await query.answer()
        return

    action = parts[0]

    if action == "menu":
        _clear_awaiting(context)
        await _show_menu(update)
        return

    if action == "id":
        await _send_or_edit(
            update,
            _format_id_text(update),
            reply_markup=InlineKeyboardMarkup([[_back_button()]]),
            parse_mode="Markdown",
        )
        return

    if not _authorized(update):
        await query.answer("Unauthorized", show_alert=True)
        return

    if action == "status":
        if len(parts) > 1 and parts[1] == "all":
            await _show_status_all(update)
        elif len(parts) > 1 and parts[1] == "refresh":
            await _show_status(update)
        elif len(parts) > 1:
            await _show_status(update, parts[1])
        else:
            await _show_status(update)
        return

    if action == "servers":
        await _show_servers(update)
        return

    if action == "pick" and len(parts) > 1:
        await _pick_server(update, parts[1])
        return

    if action == "settings":
        await _show_settings(update)
        return

    if action == "exec":
        if not _is_admin(update):
            await query.answer("Admin only", show_alert=True)
            return
        if len(parts) > 2 and parts[1] == "s":
            await _prompt_exec_command(update, context, parts[2])
        else:
            _clear_awaiting(context)
            await _show_servers(update)
        return

    if action == "al":
        await _handle_alert_callback(update, context, parts[1:])
        return

    if action == "bk":
        if not _is_admin(update):
            await query.answer("Admin only", show_alert=True)
            return
        await _handle_backup_callback(update, context, parts[1:])
        return

    if action == "admin":
        if not _is_admin(update):
            await query.answer("Admin only", show_alert=True)
            return
        sub = parts[1] if len(parts) > 1 else ""
        if sub == "users":
            _clear_awaiting(context)
            await _show_admin_users(update)
        elif sub == "servers":
            _clear_awaiting(context)
            await _show_admin_servers(update)
        elif sub == "adduser":
            _set_awaiting(context, "adduser")
            await _send_or_edit(
                update,
                "Send the chat ID to authorize.\nExample: 123456789",
                reply_markup=InlineKeyboardMarkup([[_back_button("admin", "users")]]),
            )
        elif sub == "addsrv":
            _set_awaiting(context, "addserver")
            await _send_or_edit(
                update,
                "Send server details:\nname | url | token\n\n"
                "Token is optional — one will be generated if omitted.",
                reply_markup=InlineKeyboardMarkup([[_back_button("admin", "servers")]]),
            )
        elif sub == "rmuser" and len(parts) > 2:
            try:
                uid = int(parts[2])
            except ValueError:
                return
            if uid in ADMIN_CHAT_IDS:
                await query.answer("Cannot remove an admin", show_alert=True)
            elif allowed_users.remove(uid):
                await query.answer(f"Removed {uid}")
            else:
                await query.answer("User not found", show_alert=True)
            await _show_admin_users(update)
        elif sub == "rmsrv" and len(parts) > 2:
            entry = servers.get(parts[2])
            if entry and servers.remove(entry["name"]):
                await query.answer(f"Removed {entry['name']}")
            else:
                await query.answer("Server not found", show_alert=True)
            await _show_admin_servers(update)
        elif sub == "rename" and len(parts) > 2:
            _set_awaiting(context, "rename", server_id=parts[2])
            label = _server_label(parts[2])
            await _send_or_edit(
                update,
                f"Send new name for {label}:",
                reply_markup=InlineKeyboardMarkup(
                    [[_back_button("admin", "servers")]]
                ),
            )
        return


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_awaiting(context)
    if not _authorized(update):
        await _send_or_edit(
            update,
            _format_id_text(update),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🆔 My ID", callback_data=_cb("id"))]]
            ),
            parse_mode="Markdown",
        )
        return
    await _show_menu(update)


def _can_send_alert(alert_key: str, cooldown: int) -> bool:
    last = _last_alert_at.get(alert_key, 0)
    now = time.time()
    if now - last < cooldown:
        return False
    _last_alert_at[alert_key] = now
    return True


def _evaluate_alerts(
    snapshot: ResourceSnapshot, server_id: str, label: str, cfg: dict
) -> list[str]:
    if not cfg["enabled"]:
        return []

    messages: list[str] = []
    cooldown = int(config.data["alert_cooldown_seconds"])
    show_top = config.data.get("alert_show_top_processes", True)

    if cfg["cpu_enabled"]:
        cpu_threshold = float(cfg["cpu_threshold_percent"])
        sustained = int(cfg["cpu_sustained_checks"])
        cpu_high = snapshot.cpu_percent_avg >= cpu_threshold
        streak = _cpu_high_streak.get(server_id, 0)
        if cpu_high:
            streak += 1
        else:
            streak = 0
        _cpu_high_streak[server_id] = streak

        if streak >= sustained and _can_send_alert(f"{server_id}:cpu", cooldown):
            cores = ", ".join(f"{v:.0f}%" for v in snapshot.cpu_percent_per_core)
            text = (
                f"🚨 CPU alert on {label}\n"
                f"Host: {snapshot.hostname}\n"
                f"Average CPU {snapshot.cpu_percent_avg:.1f}% "
                f">= {cpu_threshold:.0f}% for {sustained} checks.\n"
                f"Cores: {cores}"
            )
            if show_top:
                text += format_top_processes_block(snapshot, kind="cpu")
            messages.append(text)
            _cpu_high_streak[server_id] = 0
    else:
        _cpu_high_streak[server_id] = 0

    if cfg["ram_enabled"]:
        ram_limit = float(cfg["ram_threshold_percent"])
        if snapshot.ram_percent >= ram_limit and _can_send_alert(
            f"{server_id}:ram", cooldown
        ):
            text = (
                f"🚨 RAM alert on {label}\n"
                f"Host: {snapshot.hostname}\n"
                f"RAM usage {snapshot.ram_percent:.1f}% >= {ram_limit:.0f}%\n"
                f"Used: {snapshot.ram_used_gb:.2f} / {snapshot.ram_total_gb:.2f} GB"
            )
            if show_top:
                text += format_top_processes_block(snapshot, kind="ram")
            messages.append(text)

    if cfg["disk_enabled"]:
        disk_limit = float(cfg["disk_threshold_percent"])
        if snapshot.disk_percent >= disk_limit and _can_send_alert(
            f"{server_id}:disk", cooldown
        ):
            messages.append(
                f"🚨 Disk alert on {label}\n"
                f"Host: {snapshot.hostname}\n"
                f"Path {snapshot.disk_path}: {snapshot.disk_percent:.1f}% used "
                f"({snapshot.disk_used_gb:.2f} / {snapshot.disk_total_gb:.2f} GB)"
            )

    return messages


async def monitor_loop(application: Application) -> None:
    logger.info("Monitor loop running")
    while True:
        config.load()
        servers.load()
        interval = int(config.data["check_interval_seconds"])
        try:
            targets = _monitor_target_ids()
            if not targets:
                logger.warning(
                    "Monitor: no targets (enable MONITOR_LOCAL or add remote servers)"
                )
            all_alerts: list[str] = []
            for server_id in targets:
                alert_cfg = config.get_server_alerts(server_id)
                if not alert_cfg["enabled"]:
                    continue
                if not (
                    alert_cfg["cpu_enabled"]
                    or alert_cfg["ram_enabled"]
                    or alert_cfg["disk_enabled"]
                ):
                    continue
                label = _server_label(server_id)
                try:
                    snapshot = await _fetch_status(server_id)
                except RemoteAgentError:
                    logger.warning("Monitor: could not reach %s", label)
                    continue
                all_alerts.extend(
                    _evaluate_alerts(snapshot, server_id, label, alert_cfg)
                )
            if all_alerts:
                logger.info("Sending %s alert(s)", len(all_alerts))
                menu = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("📊 Open menu", callback_data=_cb("menu"))]]
                )
                recipients = _allowed_chat_ids()
                if not recipients:
                    logger.warning("Monitor: alerts ready but no allowed chat IDs")
                for chat_id in recipients:
                    for text in all_alerts:
                        try:
                            await application.bot.send_message(
                                chat_id=chat_id, text=text, reply_markup=menu
                            )
                        except Exception:
                            logger.exception(
                                "Failed to send alert to chat %s", chat_id
                            )
        except Exception:
            logger.exception("Monitor loop error")
        await asyncio.sleep(interval)


async def backup_loop(application: Application) -> None:
    global _last_backup_at
    logger.info("Backup loop running")
    while True:
        try:
            config.load()
            b = config.get_backup()
            enabled = bool(b.get("enabled"))
            path = (b.get("path") or "").strip()
            interval_min = max(1, int(b.get("interval_minutes") or 60))
            chat_id = b.get("notify_chat_id")
            if enabled and path and chat_id is not None:
                due = (time.time() - _last_backup_at) >= interval_min * 60
                if due and not _backup_running:
                    try:
                        await _send_backup_document(
                            application.bot,
                            int(chat_id),
                            path,
                            caption=(
                                f"📦 Scheduled backup of {path}\n"
                                f"Interval: every {interval_min} min"
                            ),
                        )
                    except BackupError as exc:
                        logger.warning("Scheduled backup failed: %s", exc)
                        try:
                            await application.bot.send_message(
                                chat_id=int(chat_id),
                                text=f"❌ Scheduled backup failed:\n{exc}",
                            )
                        except Exception:
                            logger.exception(
                                "Failed to notify chat %s about backup error",
                                chat_id,
                            )
                    except Exception:
                        logger.exception("Scheduled backup upload error")
        except Exception:
            logger.exception("Backup loop error")
        await asyncio.sleep(15)


async def post_init(application: Application) -> None:
    interval = int(config.data["check_interval_seconds"])
    # Prefer JobQueue when available; otherwise start after the app is running
    # so create_task is tracked correctly by PTB.
    async def _start_background() -> None:
        while not application.running:
            await asyncio.sleep(0.05)
        application.create_task(monitor_loop(application), name="monitor_loop")
        application.create_task(backup_loop(application), name="backup_loop")
        logger.info("Background monitoring started (every %ss).", interval)
        logger.info("Background backup scheduler started.")

    asyncio.create_task(_start_background(), name="start_background")


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env")
    if not ADMIN_CHAT_IDS:
        raise SystemExit(
            "Set ADMIN_CHAT_IDS or ALLOWED_CHAT_IDS in .env (comma-separated chat IDs)"
        )

    application = Application.builder().token(token).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text_input)
    )

    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
