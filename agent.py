import logging
import os
import socket
import time

import httpx
from dotenv import load_dotenv

from system_stats import sample_resources, snapshot_to_dict

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

AGENT_TOKEN = os.getenv("AGENT_TOKEN", "")
AGENT_NAME = os.getenv("AGENT_NAME", "").strip()
BOT_SERVER_HOST = os.getenv("BOT_SERVER_HOST", "").strip()
BOT_SERVER_PORT = int(os.getenv("BOT_SERVER_PORT", "8766"))
DISK_PATH = os.getenv("DISK_PATH", "/")
PUSH_INTERVAL = int(os.getenv("PUSH_INTERVAL", "30"))


def _hub_url(path: str) -> str:
    return f"http://{BOT_SERVER_HOST}:{BOT_SERVER_PORT}{path}"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {AGENT_TOKEN}"}


def register() -> bool:
    payload = {
        "name": AGENT_NAME,
        "hostname": socket.gethostname(),
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(
                _hub_url("/agent/register"),
                headers=_headers(),
                json=payload,
            )
            if response.status_code == 200:
                data = response.json()
                logger.info("Registered as %s (id %s)", data.get("name"), data.get("id"))
                return True
            logger.error("Register failed (%s): %s", response.status_code, response.text)
    except httpx.HTTPError as exc:
        logger.error("Could not reach bot server: %s", exc)
    return False


def push_snapshot() -> bool:
    snapshot = sample_resources(DISK_PATH, 1.0)
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                _hub_url("/agent/push"),
                headers=_headers(),
                json=snapshot_to_dict(snapshot),
            )
            if response.status_code == 200:
                return True
            logger.warning("Push rejected (%s): %s", response.status_code, response.text)
    except httpx.HTTPError as exc:
        logger.error("Push failed: %s", exc)
    return False


def main() -> None:
    if not AGENT_TOKEN:
        raise SystemExit("Set AGENT_TOKEN in .env")
    if not AGENT_NAME:
        raise SystemExit("Set AGENT_NAME in .env")
    if not BOT_SERVER_HOST:
        raise SystemExit("Set BOT_SERVER_HOST in .env (IP or hostname of the bot server)")

    logger.info(
        "Agent %s connecting to %s:%s every %ss",
        AGENT_NAME,
        BOT_SERVER_HOST,
        BOT_SERVER_PORT,
        PUSH_INTERVAL,
    )

    while not register():
        logger.info("Retry register in 10s ...")
        time.sleep(10)

    while True:
        if not push_snapshot():
            logger.info("Retry register after push failure ...")
            register()
        time.sleep(PUSH_INTERVAL)


if __name__ == "__main__":
    main()
