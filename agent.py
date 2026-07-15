import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from dotenv import load_dotenv

from command_runner import run_command
from system_stats import sample_resources, snapshot_to_dict

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

AGENT_TOKEN = os.getenv("AGENT_TOKEN", "")
DISK_PATH = os.getenv("DISK_PATH", "/")
AGENT_HOST = os.getenv("AGENT_HOST", "0.0.0.0")
AGENT_PORT = int(os.getenv("AGENT_PORT", "8765"))
EXEC_TIMEOUT_SECONDS = float(os.getenv("EXEC_TIMEOUT_SECONDS", "30"))
EXEC_MAX_OUTPUT = int(os.getenv("EXEC_MAX_OUTPUT", "3500"))
EXEC_ENABLED = os.getenv("EXEC_ENABLED", "false").lower() in ("1", "true", "yes")


class AgentHandler(BaseHTTPRequestHandler):
    server_version = "OnHandServerAgent/1.0"

    def log_message(self, format: str, *args) -> None:
        logger.info("%s - %s", self.address_string(), format % args)

    def _authorized(self) -> bool:
        if not AGENT_TOKEN:
            return False
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {AGENT_TOKEN}"

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reject_unauthorized(self) -> None:
        self._send_json(401, {"error": "unauthorized"})

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            if not self._authorized():
                self._reject_unauthorized()
                return
            self._send_json(200, {"status": "ok"})
            return

        if path == "/status":
            if not self._authorized():
                self._reject_unauthorized()
                return
            try:
                snapshot = sample_resources(DISK_PATH, 1.0)
                self._send_json(200, snapshot_to_dict(snapshot))
            except Exception as exc:
                logger.exception("Failed to sample resources")
                self._send_json(500, {"error": str(exc)})
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/exec":
            self._send_json(404, {"error": "not found"})
            return
        if not EXEC_ENABLED:
            self._send_json(403, {"error": "command execution disabled"})
            return
        if not self._authorized():
            self._reject_unauthorized()
            return
        try:
            payload = self._read_json_body()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
            return

        command = str(payload.get("command", "")).strip()
        if not command:
            self._send_json(400, {"error": "missing command"})
            return

        timeout = float(payload.get("timeout", EXEC_TIMEOUT_SECONDS))
        max_output = int(payload.get("max_output", EXEC_MAX_OUTPUT))
        try:
            result = run_command(command, timeout=timeout, max_output=max_output)
            self._send_json(
                200,
                {
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                },
            )
        except Exception as exc:
            logger.exception("Failed to run command")
            self._send_json(500, {"error": str(exc)})


def main() -> None:
    if not AGENT_TOKEN:
        raise SystemExit("Set AGENT_TOKEN in .env")

    server = ThreadingHTTPServer((AGENT_HOST, AGENT_PORT), AgentHandler)
    logger.info("Agent listening on %s:%s", AGENT_HOST, AGENT_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Agent stopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
