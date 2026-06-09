import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from system_stats import ResourceSnapshot, snapshot_from_dict, snapshot_to_dict

if TYPE_CHECKING:
    from servers_store import ServersStore

logger = logging.getLogger(__name__)

OFFLINE_AFTER_SECONDS = 90


class RemoteAgentError(Exception):
    pass


class AgentHub:
    def __init__(self, servers: "ServersStore") -> None:
        self._servers = servers
        self._lock = threading.Lock()
        self._snapshots: dict[str, tuple[ResourceSnapshot, float]] = {}

    def get_snapshot(self, server_id: str) -> ResourceSnapshot | None:
        entry = self._servers.get(server_id)
        if entry is None:
            return None
        with self._lock:
            cached = self._snapshots.get(entry["token"])
            return cached[0] if cached else None

    def is_online(self, server_id: str, timeout: float = OFFLINE_AFTER_SECONDS) -> bool:
        entry = self._servers.get(server_id)
        if entry is None:
            return False
        last_seen = entry.get("last_seen")
        if last_seen is None:
            with self._lock:
                cached = self._snapshots.get(entry["token"])
                if cached:
                    return (time.time() - cached[1]) < timeout
            return False
        return (time.time() - float(last_seen)) < timeout

    def _store_push(self, token: str, snapshot: ResourceSnapshot) -> None:
        now = time.time()
        with self._lock:
            self._snapshots[token] = (snapshot, now)
        entry = self._servers.get_by_token(token)
        if entry:
            self._servers.update_agent_meta(
                entry["id"],
                last_seen=now,
                hostname=snapshot.hostname,
            )

    def _register(self, token: str, name: str, hostname: str = "") -> dict:
        return self._servers.ensure_agent(token, name, hostname)


class _HubHandler(BaseHTTPRequestHandler):
    hub: AgentHub

    def log_message(self, format: str, *args) -> None:
        logger.info("%s - %s", self.address_string(), format % args)

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return None

    def _token(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return None

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        token = self._token()
        if not token:
            self._send_json(401, {"error": "missing token"})
            return

        if path == "/agent/register":
            data = self._read_json()
            if data is None:
                self._send_json(400, {"error": "invalid json"})
                return
            name = str(data.get("name", "")).strip()
            if not name:
                self._send_json(400, {"error": "name required"})
                return
            hostname = str(data.get("hostname", "")).strip()
            try:
                entry = self.hub._register(token, name, hostname)
            except ValueError as exc:
                self._send_json(409, {"error": str(exc)})
                return
            self._send_json(200, {"status": "ok", "id": entry["id"], "name": entry["name"]})
            return

        if path == "/agent/push":
            data = self._read_json()
            if data is None:
                self._send_json(400, {"error": "invalid json"})
                return
            entry = self.hub._servers.get_by_token(token)
            if entry is None:
                self._send_json(401, {"error": "unknown agent — register first"})
                return
            try:
                snapshot = snapshot_from_dict(data)
            except (KeyError, TypeError, ValueError):
                self._send_json(400, {"error": "malformed snapshot"})
                return
            self.hub._store_push(token, snapshot)
            self._send_json(200, {"status": "ok"})
            return

        self._send_json(404, {"error": "not found"})


def start_agent_hub(
    servers: "ServersStore",
    host: str,
    port: int,
) -> tuple[AgentHub, ThreadingHTTPServer, threading.Thread]:
    hub = AgentHub(servers)

    class Handler(_HubHandler):
        pass

    Handler.hub = hub
    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="agent-hub")
    thread.start()
    logger.info("Agent hub listening on %s:%s", host, port)
    return hub, server, thread
