import asyncio
import logging
import os
import socket
import time
from pathlib import Path

import psutil
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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


def _parse_chat_ids(env_name: str) -> set[int]:
    return {
        int(chat_id.strip())
        for chat_id in os.getenv(env_name, "").split(",")
        if chat_id.strip()
    }


# Admins can manage users; fall back to ALLOWED_CHAT_IDS for existing installs.
ADMIN_CHAT_IDS = _parse_chat_ids("ADMIN_CHAT_IDS") or _parse_chat_ids(
    "ALLOWED_CHAT_IDS"
)

# Runtime alert state (not persisted)
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


def _resolve_server_id(name: str) -> str | None:
    if name.lower() == "local" and MONITOR_LOCAL:
        return LOCAL_SERVER_ID
    match = servers.get_by_name(name)
    return match["id"] if match else None


def _server_label(server_id: str) -> str:
    if server_id == LOCAL_SERVER_ID:
        return _local_display_name()
    entry = servers.get(server_id)
    return entry["name"] if entry else server_id


def _resolve_selection(chat_id: int | None) -> str | None:
    if chat_id is not None:
        selected = servers.get_selection(chat_id)
        if selected and (
            selected == LOCAL_SERVER_ID
            or servers.get(selected) is not None
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


async def _deny(update: Update) -> None:
    if update.message:
        await update.message.reply_text(
            "Unauthorized. Send /id to get your chat ID, then ask an admin to run "
            "/adduser <id>."
        )


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
        f"Disk ({data['disk_path']}): >= {data['disk_threshold_percent']}% used"
    )


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not update.message or chat is None:
        return
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
            "\nYou are not authorized yet. Send this ID to an admin so they can run "
            "/adduser <id>."
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        await _deny(update)
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /adduser <chat_id>")
        return
    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Chat ID must be a number.")
        return
    if chat_id in ADMIN_CHAT_IDS:
        await update.message.reply_text(f"{chat_id} is already an admin.")
        return
    if allowed_users.add(chat_id):
        await update.message.reply_text(f"Added user {chat_id}.")
    else:
        await update.message.reply_text(f"{chat_id} was already authorized.")


async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        await _deny(update)
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /removeuser <chat_id>")
        return
    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Chat ID must be a number.")
        return
    if chat_id in ADMIN_CHAT_IDS:
        await update.message.reply_text("Cannot remove an admin from .env.")
        return
    if allowed_users.remove(chat_id):
        await update.message.reply_text(f"Removed user {chat_id}.")
    else:
        await update.message.reply_text(f"{chat_id} is not in the allowed list.")


async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        await _deny(update)
        return
    lines = ["Allowed users\n"]
    lines.append("Admins (from .env):")
    for chat_id in sorted(ADMIN_CHAT_IDS):
        lines.append(f"  {chat_id}")
    added = sorted(allowed_users.chat_ids)
    lines.append("\nAdded via bot:")
    if added:
        for chat_id in added:
            lines.append(f"  {chat_id}")
    else:
        lines.append("  (none)")
    await update.message.reply_text("\n".join(lines))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    help_text = (
        "Server resource monitor\n\n"
        "Commands:\n"
        "/status - CPU, RAM, disk for selected server\n"
        "/status all - brief overview of every server\n"
        "/servers - list servers and your current selection\n"
        "/use <name> - select server (use 'local' for this host)\n"
        "/config - show alert thresholds\n"
        "/alerts on|off - enable or disable alerts\n"
        "/setcpu <percent> - CPU alert when all cores stay high\n"
        "/setcpu_checks <count> - how many checks in a row (default 3)\n"
        "/setram <percent> - RAM alert by percent (use 0 to disable)\n"
        "/setramgb <gb> - RAM alert by used GB (use 0 to disable)\n"
        "/setdisk <percent> - disk usage alert\n"
        "/setdiskpath <path> - disk to monitor (e.g. / or /home)\n"
        "/setinterval <seconds> - how often to check\n"
        "/setcooldown <seconds> - minimum time between duplicate alerts\n"
        "/id - show your chat ID"
    )
    if _is_admin(update):
        help_text += (
            "\n\nAdmin:\n"
            "/adduser <id> - allow a user to use the bot\n"
            "/removeuser <id> - revoke access\n"
            "/users - list allowed users\n"
            "/addserver <name> <url> [token] - register a remote agent\n"
            "/removeserver <name> - unregister a remote server\n"
            "/renameserver <old> <new> - rename a server"
        )
    await update.message.reply_text(help_text)


async def list_servers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    chat_id = _chat_id(update)
    selected = _resolve_selection(chat_id)
    lines = ["Registered servers\n"]
    if MONITOR_LOCAL:
        marker = " ← selected" if selected == LOCAL_SERVER_ID else ""
        lines.append(f"  local — {_local_display_name()}{marker}")
    remote = servers.servers
    if remote:
        for entry in remote:
            marker = " ← selected" if selected == entry["id"] else ""
            online = await ping_agent(entry["url"], entry["token"])
            state = "online" if online else "offline"
            lines.append(f"  {entry['name']} — {state}{marker}")
    elif not MONITOR_LOCAL:
        lines.append("  (none — add with /addserver)")
    if len(_monitor_target_ids()) > 1 and selected is None:
        lines.append("\nUse /use <name> to pick a server for /status.")
    await update.message.reply_text("\n".join(lines))


async def use_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /use <name>  (use 'local' for this host)")
        return
    server_id = _resolve_server_id(context.args[0])
    if server_id is None:
        await update.message.reply_text(f"Unknown server: {context.args[0]}")
        return
    chat_id = _chat_id(update)
    if chat_id is None:
        return
    servers.set_selection(chat_id, server_id)
    await update.message.reply_text(f"Selected server: {_server_label(server_id)}")


async def add_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        await _deny(update)
        return
    if len(context.args) not in {2, 3}:
        await update.message.reply_text(
            "Usage: /addserver <name> <url> [token]\n"
            "Example: /addserver prod http://10.0.0.5:8765 my-secret-token"
        )
        return
    name, url = context.args[0], context.args[1]
    token = context.args[2] if len(context.args) == 3 else ServersStore.generate_token()
    try:
        entry = servers.add(name, url, token)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    online = await ping_agent(entry["url"], entry["token"])
    status_text = "reachable" if online else "not reachable (check URL, firewall, agent)"
    await update.message.reply_text(
        f"Added server {entry['name']}\n"
        f"URL: {entry['url']}\n"
        f"Token: {entry['token']}\n"
        f"Agent: {status_text}\n\n"
        "Set the same AGENT_TOKEN on the remote host's .env."
    )


async def remove_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        await _deny(update)
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /removeserver <name>")
        return
    if servers.remove(context.args[0]):
        await update.message.reply_text(f"Removed server {context.args[0]}.")
    else:
        await update.message.reply_text(f"Server not found: {context.args[0]}")


async def rename_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        await _deny(update)
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /renameserver <old_name> <new_name>")
        return
    try:
        if servers.rename(context.args[0], context.args[1]):
            await update.message.reply_text(
                f"Renamed {context.args[0]} → {context.args[1]}."
            )
        else:
            await update.message.reply_text(f"Server not found: {context.args[0]}")
    except ValueError as exc:
        await update.message.reply_text(str(exc))


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    if context.args and context.args[0].lower() == "all":
        targets = _monitor_target_ids()
        if not targets:
            await update.message.reply_text("No servers configured.")
            return
        lines = ["All servers\n"]
        for server_id in targets:
            label = _server_label(server_id)
            try:
                snapshot = await _fetch_status(server_id)
                lines.append(
                    f"{label}: CPU {snapshot.cpu_percent_avg:.0f}%, "
                    f"RAM {snapshot.ram_percent:.0f}%, "
                    f"Disk {snapshot.disk_percent:.0f}%"
                )
            except RemoteAgentError:
                lines.append(f"{label}: unreachable")
        await update.message.reply_text("\n".join(lines))
        return

    server_id = _resolve_selection(_chat_id(update))
    if server_id is None:
        await update.message.reply_text(
            "Multiple servers available. Use /servers to list them, "
            "then /use <name> to select one.\n"
            "Or send /status all for a quick overview."
        )
        return
    try:
        snapshot = await _fetch_status(server_id)
    except RemoteAgentError as exc:
        await update.message.reply_text(
            f"Could not reach {_server_label(server_id)}: {exc}"
        )
        return
    label = _server_label(server_id)
    display_name = (
        None if server_id == LOCAL_SERVER_ID and not LOCAL_SERVER_NAME else label
    )
    await update.message.reply_text(
        format_status(snapshot, display_name=display_name)
    )


async def show_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    await update.message.reply_text(_format_config())


async def set_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    if len(context.args) != 1 or context.args[0].lower() not in {"on", "off"}:
        await update.message.reply_text("Usage: /alerts on|off")
        return
    enabled = context.args[0].lower() == "on"
    config.update(alerts_enabled=enabled)
    await update.message.reply_text(f"Alerts turned {'ON' if enabled else 'OFF'}.")


async def set_cpu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /setcpu <percent>")
        return
    try:
        value = float(context.args[0])
    except ValueError:
        await update.message.reply_text("Percent must be a number.")
        return
    if not 1 <= value <= 100:
        await update.message.reply_text("Percent must be between 1 and 100.")
        return
    config.update(cpu_threshold_percent=value)
    await update.message.reply_text(
        f"CPU alert set: all cores >= {value:.0f}% for "
        f"{config.data['cpu_sustained_checks']} checks."
    )


async def set_cpu_checks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /setcpu_checks <count>")
        return
    try:
        value = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Count must be an integer.")
        return
    if value < 1:
        await update.message.reply_text("Count must be at least 1.")
        return
    config.update(cpu_sustained_checks=value)
    await update.message.reply_text(f"CPU sustained checks set to {value}.")


async def set_ram(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /setram <percent> (0 to disable)")
        return
    try:
        value = float(context.args[0])
    except ValueError:
        await update.message.reply_text("Percent must be a number.")
        return
    if value == 0:
        config.update(ram_threshold_percent=None)
        await update.message.reply_text("RAM percent alert disabled.")
        return
    if not 1 <= value <= 100:
        await update.message.reply_text("Percent must be between 1 and 100.")
        return
    config.update(ram_threshold_percent=value)
    await update.message.reply_text(f"RAM percent alert set to {value:.0f}%.")


async def set_ram_gb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /setramgb <gb> (0 to disable)")
        return
    try:
        value = float(context.args[0])
    except ValueError:
        await update.message.reply_text("GB must be a number.")
        return
    if value == 0:
        config.update(ram_threshold_gb=None)
        await update.message.reply_text("RAM GB alert disabled.")
        return
    if value <= 0:
        await update.message.reply_text("GB must be greater than 0.")
        return
    config.update(ram_threshold_gb=value)
    await update.message.reply_text(f"RAM GB alert set to {value:.2f} GB used.")


async def set_disk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /setdisk <percent>")
        return
    try:
        value = float(context.args[0])
    except ValueError:
        await update.message.reply_text("Percent must be a number.")
        return
    if not 1 <= value <= 100:
        await update.message.reply_text("Percent must be between 1 and 100.")
        return
    config.update(disk_threshold_percent=value)
    await update.message.reply_text(f"Disk alert set to {value:.0f}% used.")


async def set_disk_path(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /setdiskpath <path>")
        return
    path = context.args[0]
    try:
        _ = psutil.disk_usage(path).total
    except Exception:
        await update.message.reply_text(f"Invalid or inaccessible path: {path}")
        return
    config.update(disk_path=path)
    await update.message.reply_text(f"Disk path set to {path}")


async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /setinterval <seconds>")
        return
    try:
        value = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Seconds must be an integer.")
        return
    if value < 10:
        await update.message.reply_text("Interval must be at least 10 seconds.")
        return
    config.update(check_interval_seconds=value)
    await update.message.reply_text(f"Check interval set to {value}s.")


async def set_cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /setcooldown <seconds>")
        return
    try:
        value = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Seconds must be an integer.")
        return
    if value < 60:
        await update.message.reply_text("Cooldown must be at least 60 seconds.")
        return
    config.update(alert_cooldown_seconds=value)
    await update.message.reply_text(f"Alert cooldown set to {value}s.")


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
                for chat_id in _allowed_chat_ids():
                    for text in all_alerts:
                        try:
                            await application.bot.send_message(
                                chat_id=chat_id, text=text
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

    application.add_handler(CommandHandler("id", show_id))
    application.add_handler(CommandHandler("adduser", add_user))
    application.add_handler(CommandHandler("removeuser", remove_user))
    application.add_handler(CommandHandler("users", list_users))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("servers", list_servers))
    application.add_handler(CommandHandler("use", use_server))
    application.add_handler(CommandHandler("addserver", add_server))
    application.add_handler(CommandHandler("removeserver", remove_server))
    application.add_handler(CommandHandler("renameserver", rename_server))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("config", show_config))
    application.add_handler(CommandHandler("alerts", set_alerts))
    application.add_handler(CommandHandler("setcpu", set_cpu))
    application.add_handler(CommandHandler("setcpu_checks", set_cpu_checks))
    application.add_handler(CommandHandler("setram", set_ram))
    application.add_handler(CommandHandler("setramgb", set_ram_gb))
    application.add_handler(CommandHandler("setdisk", set_disk))
    application.add_handler(CommandHandler("setdiskpath", set_disk_path))
    application.add_handler(CommandHandler("setinterval", set_interval))
    application.add_handler(CommandHandler("setcooldown", set_cooldown))

    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()