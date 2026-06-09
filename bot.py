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
from system_stats import ResourceSnapshot, format_status, sample_resources

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


def _settings_keyboard() -> InlineKeyboardMarkup:
    data = config.data
    alerts_on = data["alerts_enabled"]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"🔔 Alerts: {'ON' if alerts_on else 'OFF'}",
                    callback_data=_cb("cfg", "alerts", "off" if alerts_on else "on"),
                )
            ],
            [
                InlineKeyboardButton("CPU 80%", callback_data=_cb("cfg", "cpu", "80")),
                InlineKeyboardButton("CPU 90%", callback_data=_cb("cfg", "cpu", "90")),
                InlineKeyboardButton("CPU 95%", callback_data=_cb("cfg", "cpu", "95")),
            ],
            [
                InlineKeyboardButton("Checks ×3", callback_data=_cb("cfg", "cpuck", "3")),
                InlineKeyboardButton("Checks ×5", callback_data=_cb("cfg", "cpuck", "5")),
                InlineKeyboardButton(
                    "Checks ×10", callback_data=_cb("cfg", "cpuck", "10")
                ),
            ],
            [
                InlineKeyboardButton("RAM 80%", callback_data=_cb("cfg", "ram", "80")),
                InlineKeyboardButton("RAM 90%", callback_data=_cb("cfg", "ram", "90")),
                InlineKeyboardButton("RAM off", callback_data=_cb("cfg", "ram", "0")),
            ],
            [
                InlineKeyboardButton("RAM 4GB", callback_data=_cb("cfg", "ramgb", "4")),
                InlineKeyboardButton("RAM 8GB", callback_data=_cb("cfg", "ramgb", "8")),
                InlineKeyboardButton(
                    "RAM 16GB", callback_data=_cb("cfg", "ramgb", "16")
                ),
                InlineKeyboardButton("RAM GB off", callback_data=_cb("cfg", "ramgb", "0")),
            ],
            [
                InlineKeyboardButton("Disk 80%", callback_data=_cb("cfg", "disk", "80")),
                InlineKeyboardButton("Disk 90%", callback_data=_cb("cfg", "disk", "90")),
                InlineKeyboardButton("Disk 95%", callback_data=_cb("cfg", "disk", "95")),
            ],
            [
                InlineKeyboardButton("Every 30s", callback_data=_cb("cfg", "int", "30")),
                InlineKeyboardButton("Every 60s", callback_data=_cb("cfg", "int", "60")),
                InlineKeyboardButton(
                    "Every 120s", callback_data=_cb("cfg", "int", "120")
                ),
            ],
            [
                InlineKeyboardButton("CD 5m", callback_data=_cb("cfg", "cd", "300")),
                InlineKeyboardButton("CD 10m", callback_data=_cb("cfg", "cd", "600")),
                InlineKeyboardButton("CD 30m", callback_data=_cb("cfg", "cd", "1800")),
            ],
            [_back_button()],
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


def _format_config() -> str:
    data = config.data
    ram_percent = data["ram_threshold_percent"]
    ram_gb = data["ram_threshold_gb"]
    ram_rule = []
    if ram_percent is not None:
        ram_rule.append(f"{ram_percent}%")
    if ram_gb is not None:
        ram_rule.append(f"{ram_gb} GB used")
    ram_text = " or ".join(ram_rule) if ram_rule else "disabled"

    return (
        "⚙️ Alert settings\n\n"
        f"Alerts: {'ON' if data['alerts_enabled'] else 'OFF'}\n"
        f"Check interval: {data['check_interval_seconds']}s\n"
        f"Alert cooldown: {data['alert_cooldown_seconds']}s\n\n"
        f"CPU: all cores >= {data['cpu_threshold_percent']}% for "
        f"{data['cpu_sustained_checks']} checks in a row\n"
        f"RAM: {ram_text}\n"
        f"Disk ({data['disk_path']}): >= {data['disk_threshold_percent']}% used\n\n"
        "Tap a button below to change a value."
    )


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
    await _send_or_edit(
        update, _format_config(), reply_markup=_settings_keyboard()
    )


async def _apply_config_change(update: Update, parts: list[str]) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    if len(parts) < 2:
        return
    kind, value = parts[0], parts[1]
    msg = ""

    if kind == "alerts":
        enabled = value == "on"
        config.update(alerts_enabled=enabled)
        msg = f"Alerts turned {'ON' if enabled else 'OFF'}."
    elif kind == "cpu":
        v = float(value)
        config.update(cpu_threshold_percent=v)
        msg = f"CPU threshold set to {v:.0f}%."
    elif kind == "cpuck":
        v = int(value)
        config.update(cpu_sustained_checks=v)
        msg = f"CPU sustained checks set to {v}."
    elif kind == "ram":
        v = float(value)
        if v == 0:
            config.update(ram_threshold_percent=None)
            msg = "RAM percent alert disabled."
        else:
            config.update(ram_threshold_percent=v)
            msg = f"RAM percent alert set to {v:.0f}%."
    elif kind == "ramgb":
        v = float(value)
        if v == 0:
            config.update(ram_threshold_gb=None)
            msg = "RAM GB alert disabled."
        else:
            config.update(ram_threshold_gb=v)
            msg = f"RAM GB alert set to {v:.2f} GB."
    elif kind == "disk":
        v = float(value)
        config.update(disk_threshold_percent=v)
        msg = f"Disk alert set to {v:.0f}%."
    elif kind == "int":
        v = int(value)
        config.update(check_interval_seconds=v)
        msg = f"Check interval set to {v}s."
    elif kind == "cd":
        v = int(value)
        config.update(alert_cooldown_seconds=v)
        msg = f"Alert cooldown set to {v}s."

    await _send_or_edit(
        update,
        f"{msg}\n\n{_format_config()}",
        reply_markup=_settings_keyboard(),
    )


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

    if action == "cfg":
        await _apply_config_change(update, parts[1:])
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


def _evaluate_alerts(snapshot: ResourceSnapshot, server_id: str, label: str) -> list[str]:
    data = config.data
    messages: list[str] = []
    cooldown = int(data["alert_cooldown_seconds"])

    cpu_threshold = float(data["cpu_threshold_percent"])
    sustained = int(data["cpu_sustained_checks"])
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
        messages.append(
            f"🚨 CPU alert on {label}\n"
            f"Host: {snapshot.hostname}\n"
            f"All cores >= {cpu_threshold:.0f}% for {sustained} checks.\n"
            f"Cores: {cores}\n"
            f"Average: {snapshot.cpu_percent_avg:.1f}%"
        )
        _cpu_high_streak[server_id] = 0

    ram_percent_limit = data["ram_threshold_percent"]
    ram_gb_limit = data["ram_threshold_gb"]
    ram_triggered = False
    if ram_percent_limit is not None and snapshot.ram_percent >= float(ram_percent_limit):
        ram_triggered = True
        reason = f"RAM usage {snapshot.ram_percent:.1f}% >= {ram_percent_limit:.0f}%"
    elif ram_gb_limit is not None and snapshot.ram_used_gb >= float(ram_gb_limit):
        ram_triggered = True
        reason = (
            f"RAM used {snapshot.ram_used_gb:.2f} GB >= {float(ram_gb_limit):.2f} GB"
        )
    else:
        reason = ""

    if ram_triggered and _can_send_alert(f"{server_id}:ram", cooldown):
        messages.append(
            f"🚨 RAM alert on {label}\n"
            f"Host: {snapshot.hostname}\n"
            f"{reason}\n"
            f"Total: {snapshot.ram_total_gb:.2f} GB"
        )

    disk_limit = float(data["disk_threshold_percent"])
    if snapshot.disk_percent >= disk_limit and _can_send_alert(f"{server_id}:disk", cooldown):
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
            if config.data["alerts_enabled"]:
                all_alerts: list[str] = []
                for server_id in _monitor_target_ids():
                    label = _server_label(server_id)
                    try:
                        snapshot = await _fetch_status(server_id)
                    except RemoteAgentError:
                        logger.warning("Monitor: could not reach %s", label)
                        continue
                    all_alerts.extend(_evaluate_alerts(snapshot, server_id, label))
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
