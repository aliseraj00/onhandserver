import json
import secrets
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any


class ServersStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"servers": [], "user_selection": {}}
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                self.save()
                return
            with self.path.open(encoding="utf-8") as handle:
                stored = json.load(handle)
            servers = stored.get("servers", [])
            for entry in servers:
                entry.pop("url", None)
            self._data = {
                "servers": servers,
                "user_selection": {
                    str(k): v for k, v in stored.get("user_selection", {}).items()
                },
            }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(self._data, handle, indent=2)

    @property
    def servers(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self._data["servers"])

    def get(self, server_id: str) -> dict[str, Any] | None:
        with self._lock:
            for server in self._data["servers"]:
                if server["id"] == server_id:
                    return deepcopy(server)
        return None

    def get_by_name(self, name: str) -> dict[str, Any] | None:
        needle = name.strip().lower()
        with self._lock:
            for server in self._data["servers"]:
                if server["name"].lower() == needle:
                    return deepcopy(server)
        return None

    def get_by_token(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            for server in self._data["servers"]:
                if server["token"] == token:
                    return deepcopy(server)
        return None

    def create_agent(self, name: str, token: str | None = None) -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ValueError("Name cannot be empty")
        token = token or self.generate_token()
        with self._lock:
            for server in self._data["servers"]:
                if server["name"].lower() == name.lower():
                    raise ValueError(f"Server name already in use: {server['name']}")
                if server["token"] == token:
                    raise ValueError("Token already in use")
            entry = {
                "id": uuid.uuid4().hex[:12],
                "name": name,
                "token": token,
                "last_seen": None,
                "hostname": "",
            }
            self._data["servers"].append(entry)
            self.save()
            return deepcopy(entry)

    def ensure_agent(self, token: str, name: str, hostname: str = "") -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ValueError("Name cannot be empty")
        with self._lock:
            for server in self._data["servers"]:
                if server["token"] == token:
                    if hostname:
                        server["hostname"] = hostname
                    self.save()
                    return deepcopy(server)
            for server in self._data["servers"]:
                if server["name"].lower() == name.lower():
                    raise ValueError(f"Name already in use: {server['name']}")
            entry = {
                "id": uuid.uuid4().hex[:12],
                "name": name,
                "token": token,
                "last_seen": None,
                "hostname": hostname,
            }
            self._data["servers"].append(entry)
            self.save()
            return deepcopy(entry)

    def update_agent_meta(
        self,
        server_id: str,
        *,
        last_seen: float | None = None,
        hostname: str | None = None,
    ) -> None:
        with self._lock:
            for server in self._data["servers"]:
                if server["id"] == server_id:
                    if last_seen is not None:
                        server["last_seen"] = last_seen
                    if hostname:
                        server["hostname"] = hostname
                    self.save()
                    return

    def remove(self, name: str) -> bool:
        needle = name.strip().lower()
        with self._lock:
            removed_ids = {
                s["id"] for s in self._data["servers"] if s["name"].lower() == needle
            }
            if not removed_ids:
                return False
            self._data["servers"] = [
                s for s in self._data["servers"] if s["id"] not in removed_ids
            ]
            self._data["user_selection"] = {
                chat_id: sid
                for chat_id, sid in self._data["user_selection"].items()
                if sid not in removed_ids
            }
            self.save()
            return True

    def rename(self, old_name: str, new_name: str) -> bool:
        server = self.get_by_name(old_name)
        if server is None:
            return False
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("Name cannot be empty")
        with self._lock:
            for other in self._data["servers"]:
                if other["id"] != server["id"] and other["name"].lower() == new_name.lower():
                    raise ValueError(f"Name already in use: {other['name']}")
            for entry in self._data["servers"]:
                if entry["id"] == server["id"]:
                    entry["name"] = new_name
                    break
            self.save()
            return True

    def set_selection(self, chat_id: int, server_id: str | None) -> None:
        key = str(chat_id)
        with self._lock:
            if server_id is None:
                self._data["user_selection"].pop(key, None)
            else:
                self._data["user_selection"][key] = server_id
            self.save()

    def get_selection(self, chat_id: int) -> str | None:
        with self._lock:
            return self._data["user_selection"].get(str(chat_id))

    @staticmethod
    def generate_token() -> str:
        return secrets.token_urlsafe(24)
