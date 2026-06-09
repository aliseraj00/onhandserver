import asyncio
import logging
import os
import time
from pathlib import Path

import psutil
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config_store import ConfigStore
from system_stats import format_status, sample_resources

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent / "monitor_config.json"
config = ConfigStore(CONFIG_PATH)

ALLOWED_CHAT_IDS = {
    int(chat_id.strip())
    for chat_id in os.getenv("ALLOWED_CHAT_IDS", "").split(",")
    if chat_id.strip()
}

# Runtime alert state (not persisted)
_cpu_high_streak = 0
_last_alert_at: dict[str, float] = {}


def _authorized(update: Update) -> bool:
    if not ALLOWED_CHAT_IDS:
        return False
    chat = update.effective_chat
    return chat is not None and chat.id in ALLOWED_CHAT_IDS


async def _deny(update: Update) -> None:
    if update.message:
        await update.message.reply_text(
            "Unauthorized. Add your chat ID to ALLOWED_CHAT_IDS in .env"
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    await update.message.reply_text(
        "Server resource monitor\n\n"
        "Commands:\n"
        "/status - current CPU, RAM, disk usage\n"
        "/config - show alert thresholds\n"
        "/alerts on|off - enable or disable alerts\n"
        "/setcpu <percent> - CPU alert when all cores stay high\n"
        "/setcpu_checks <count> - how many checks in a row (default 3)\n"
        "/setram <percent> - RAM alert by percent (use 0 to disable)\n"
        "/setramgb <gb> - RAM alert by used GB (use 0 to disable)\n"
        "/setdisk <percent> - disk usage alert\n"
        "/setdiskpath <path> - disk to monitor (e.g. / or /home)\n"
        "/setinterval <seconds> - how often to check\n"
        "/setcooldown <seconds> - minimum time between duplicate alerts"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    snapshot = await asyncio.to_thread(
        sample_resources, config.data["disk_path"], 1.0
    )
    await update.message.reply_text(format_status(snapshot))


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


def _evaluate_alerts(snapshot) -> list[str]:
    global _cpu_high_streak
    data = config.data
    messages: list[str] = []
    cooldown = int(data["alert_cooldown_seconds"])

    cpu_threshold = float(data["cpu_threshold_percent"])
    sustained = int(data["cpu_sustained_checks"])
    all_cores_high = bool(snapshot.cpu_percent_per_core) and all(
        core >= cpu_threshold for core in snapshot.cpu_percent_per_core
    )
    if all_cores_high:
        _cpu_high_streak += 1
    else:
        _cpu_high_streak = 0

    if _cpu_high_streak >= sustained and _can_send_alert("cpu", cooldown):
        cores = ", ".join(f"{v:.0f}%" for v in snapshot.cpu_percent_per_core)
        messages.append(
            f"🚨 CPU alert on {snapshot.hostname}\n"
            f"All cores >= {cpu_threshold:.0f}% for {sustained} checks.\n"
            f"Cores: {cores}\n"
            f"Average: {snapshot.cpu_percent_avg:.1f}%"
        )
        _cpu_high_streak = 0

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

    if ram_triggered and _can_send_alert("ram", cooldown):
        messages.append(
            f"🚨 RAM alert on {snapshot.hostname}\n"
            f"{reason}\n"
            f"Total: {snapshot.ram_total_gb:.2f} GB"
        )

    disk_limit = float(data["disk_threshold_percent"])
    if snapshot.disk_percent >= disk_limit and _can_send_alert("disk", cooldown):
        messages.append(
            f"🚨 Disk alert on {snapshot.hostname}\n"
            f"Path {snapshot.disk_path}: {snapshot.disk_percent:.1f}% used "
            f"({snapshot.disk_used_gb:.2f} / {snapshot.disk_total_gb:.2f} GB)"
        )

    return messages


async def monitor_loop(application: Application) -> None:
    while True:
        config.load()
        interval = int(config.data["check_interval_seconds"])
        try:
            if config.data["alerts_enabled"]:
                snapshot = await asyncio.to_thread(
                    sample_resources, config.data["disk_path"], 1.0
                )
                alerts = _evaluate_alerts(snapshot)
                for chat_id in ALLOWED_CHAT_IDS:
                    for text in alerts:
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
    if not ALLOWED_CHAT_IDS:
        raise SystemExit("Set ALLOWED_CHAT_IDS in .env (comma-separated chat IDs)")

    application = Application.builder().token(token).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
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
