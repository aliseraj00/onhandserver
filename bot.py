import asyncio
import logging
import os
import socket
import time
from pathlib import Path

import psutil
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
from config_store import ConfigStore
from remote_client import RemoteAgentError, fetch_remote_status, ping_agent
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
    targets.extend(server["id"] for server in servers.servers)
    return targets


def _server_label(server_id: str) -> str:
    if server_id == LOCAL_SERVER_ID:
        return _local_display_name()
    entry = servers.get(server_id)
    return entry["name"] if entry else server_id


def _resolve_selection(chat_id: int | None) -> str | None:
    if chat_id is not None:
        selected = servers.get_selection(chat_id)
        if selected and (
            selected == LOCAL_SERVER_ID or servers.get(selected) is not None
        ):
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
    return await fetch_remote_status(entry["url"], entry["token"])


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
    ram_pct = cfg["ram_threshold_percent"]
    ram_gb = cfg["ram_threshold_gb"]
    ram_lines: list[str] = []
    if ram_pct is not None:
        ram_lines.append(f"  • Percent: {ram_pct:.0f}%")
    if ram_gb is not None:
        ram_lines.append(f"  • Used GB: {ram_gb:.2f} GB")
    if not ram_lines:
        ram_lines.append("  • (set a threshold below)")

    return (
        f"🔔 Alerts — {label}\n\n"
        f"Master switch: {_on_off(cfg['enabled'])}\n\n"
        f"CPU: {_on_off(cfg['cpu_enabled'])} — "
        f"{cfg['cpu_threshold_percent']:.0f}% for {cfg['cpu_sustained_checks']} checks\n"
        f"RAM: {_on_off(cfg['ram_enabled'])}\n"
        + "\n".join(ram_lines)
        + f"\nDisk: {_on_off(cfg['disk_enabled'])} — "
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
                InlineKeyboardButton("% off", callback_data=_cb("al", "s", server_id, "r", "p", "0")),
            ],
            [
                InlineKeyboardButton("4 GB", callback_data=_cb("al", "s", server_id, "r", "g", "4")),
                InlineKeyboardButton("8 GB", callback_data=_cb("al", "s", server_id, "r", "g", "8")),
                InlineKeyboardButton("16 GB", callback_data=_cb("al", "s", server_id, "r", "g", "16")),
                InlineKeyboardButton("GB off", callback_data=_cb("al", "s", server_id, "r", "g", "0")),
            ],
            [
                InlineKeyboardButton(
                    "✏️ Custom %", callback_data=_cb("al", "s", server_id, "r", "u", "p")
                ),
                InlineKeyboardButton(
                    "✏️ Custom GB", callback_data=_cb("al", "s", server_id, "r", "u", "g")
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
    for entry in servers.servers:
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


def _status_actions_keyboard(server_id: str | None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔄 Refresh", callback_data=_cb("status", "refresh"))],
        [
            InlineKeyboardButton("🖥 Servers", callback_data=_cb("servers")),
            _back_button(),
        ],
    ]
    if server_id:
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
        reply_markup=_status_actions_keyboard(server_id),
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
    lines = ["Registered servers\nTap a server to select it and view status.\n"]
    if MONITOR_LOCAL:
        marker = " ← selected" if selected == LOCAL_SERVER_ID else ""
        lines.append(f"  local — {_local_display_name()}{marker}")
    for entry in servers.servers:
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
    await _show_status(update, server_id)


async def _show_settings(update: Update) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    targets = _monitor_target_ids()
    if len(targets) == 1:
        await _show_server_alerts(update, targets[0])
        return
    await _send_or_edit(
        update,
        _format_alert_picker_text(),
        reply_markup=_alert_server_picker_keyboard(),
    )


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
        "Pick a preset or send a custom value."
    )
    await _send_or_edit(
        update, text, reply_markup=_cpu_alert_keyboard(server_id)
    )


async def _show_ram_alerts(update: Update, server_id: str) -> None:
    cfg = config.get_server_alerts(server_id)
    pct = cfg["ram_threshold_percent"]
    gb = cfg["ram_threshold_gb"]
    text = (
        f"RAM alerts — {_server_label(server_id)}\n\n"
        f"Status: {_on_off(cfg['ram_enabled'])}\n"
        f"Percent: {f'{pct:.0f}%' if pct is not None else 'off'}\n"
        f"Used GB: {f'{gb:.2f} GB' if gb is not None else 'off'}\n\n"
        "Alert triggers if percent OR used GB threshold is exceeded."
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


async def _handle_alert_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]
) -> None:
    if not _authorized(update):
        await _deny(update)
        return

    if not parts:
        await _show_settings(update)
        return

    if parts[0] == "g":
        await _handle_global_alert_callback(update, context, parts[1:])
        return

    if parts[0] != "s" or len(parts) < 2:
        await _show_settings(update)
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
        val = float(parts[1])
        changes: dict = {"ram_enabled": True}
        changes["ram_threshold_percent"] = None if val == 0 else val
        config.update_server_alerts(server_id, **changes)
        await _show_ram_alerts(update, server_id)
        return

    if parts[0] == "g" and len(parts) > 1:
        val = float(parts[1])
        changes = {"ram_enabled": True}
        changes["ram_threshold_gb"] = None if val == 0 else val
        config.update_server_alerts(server_id, **changes)
        await _show_ram_alerts(update, server_id)
        return

    if parts[0] == "u" and len(parts) > 1:
        kind = parts[1]
        if kind == "p":
            _set_awaiting(context, "al_cfg_ram_pct", server_id=server_id)
            await _send_or_edit(
                update,
                f"Send RAM threshold % for {_server_label(server_id)} (1–100, or 0 to disable):",
                reply_markup=InlineKeyboardMarkup(
                    [[_back_button("al", "s", server_id, "ram")]]
                ),
            )
        elif kind == "g":
            _set_awaiting(context, "al_cfg_ram_gb", server_id=server_id)
            await _send_or_edit(
                update,
                f"Send RAM used GB threshold for {_server_label(server_id)} (>0, or 0 to disable):",
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
            lines.append(f"  • {entry['name']} — {entry['url']}")
    else:
        lines.append("  (none)")
    lines.append("\nAdd: name | url | token (token optional)")
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
            await update.message.reply_text("Enter a number.")
            return
        _clear_awaiting(context)
        if value == 0:
            config.update_server_alerts(server_id, ram_threshold_percent=None)
            await update.message.reply_text("RAM percent alert disabled.")
        elif 1 <= value <= 100:
            config.update_server_alerts(
                server_id, ram_enabled=True, ram_threshold_percent=value
            )
            await update.message.reply_text(f"RAM percent set to {value:.0f}%.")
        else:
            await update.message.reply_text("Percent must be between 1 and 100.")
            return
        await _show_ram_alerts(update, server_id)
        return

    if action == "al_cfg_ram_gb" and server_id:
        try:
            value = float(text)
        except ValueError:
            await update.message.reply_text("Enter a number.")
            return
        _clear_awaiting(context)
        if value == 0:
            config.update_server_alerts(server_id, ram_threshold_gb=None)
            await update.message.reply_text("RAM GB alert disabled.")
        elif value > 0:
            config.update_server_alerts(
                server_id, ram_enabled=True, ram_threshold_gb=value
            )
            await update.message.reply_text(f"RAM GB threshold set to {value:.2f} GB.")
        else:
            await update.message.reply_text("GB must be greater than 0.")
            return
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

    if action == "al":
        await _handle_alert_callback(update, context, parts[1:])
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
        all_cores_high = bool(snapshot.cpu_percent_per_core) and all(
            core >= cpu_threshold for core in snapshot.cpu_percent_per_core
        )
        streak = _cpu_high_streak.get(server_id, 0)
        if all_cores_high:
            streak += 1
        else:
            streak = 0
        _cpu_high_streak[server_id] = streak

        if streak >= sustained and _can_send_alert(f"{server_id}:cpu", cooldown):
            cores = ", ".join(f"{v:.0f}%" for v in snapshot.cpu_percent_per_core)
            text = (
                f"🚨 CPU alert on {label}\n"
                f"Host: {snapshot.hostname}\n"
                f"All cores >= {cpu_threshold:.0f}% for {sustained} checks.\n"
                f"Cores: {cores}\n"
                f"Average: {snapshot.cpu_percent_avg:.1f}%"
            )
            if show_top:
                text += format_top_processes_block(snapshot, kind="cpu")
            messages.append(text)
            _cpu_high_streak[server_id] = 0
    else:
        _cpu_high_streak[server_id] = 0

    if cfg["ram_enabled"]:
        ram_percent_limit = cfg["ram_threshold_percent"]
        ram_gb_limit = cfg["ram_threshold_gb"]
        ram_triggered = False
        if ram_percent_limit is not None and snapshot.ram_percent >= float(
            ram_percent_limit
        ):
            ram_triggered = True
            reason = (
                f"RAM usage {snapshot.ram_percent:.1f}% >= {ram_percent_limit:.0f}%"
            )
        elif ram_gb_limit is not None and snapshot.ram_used_gb >= float(ram_gb_limit):
            ram_triggered = True
            reason = (
                f"RAM used {snapshot.ram_used_gb:.2f} GB "
                f">= {float(ram_gb_limit):.2f} GB"
            )
        else:
            reason = ""

        if ram_triggered and _can_send_alert(f"{server_id}:ram", cooldown):
            text = (
                f"🚨 RAM alert on {label}\n"
                f"Host: {snapshot.hostname}\n"
                f"{reason}\n"
                f"Total: {snapshot.ram_total_gb:.2f} GB"
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
    while True:
        config.load()
        servers.load()
        interval = int(config.data["check_interval_seconds"])
        try:
            all_alerts: list[str] = []
            for server_id in _monitor_target_ids():
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
                menu = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("📊 Open menu", callback_data=_cb("menu"))]]
                )
                for chat_id in _allowed_chat_ids():
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


async def post_init(application: Application) -> None:
    interval = int(config.data["check_interval_seconds"])
    application.create_task(monitor_loop(application))
    logger.info("Background monitoring started (every %ss).", interval)


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
