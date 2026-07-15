import json
import logging
import secrets
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ServersStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, Any] = {"servers": [], "user_selection": {}}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.save()
            return
        with self.path.open(encoding="utf-8") as handle:
            stored = json.load(handle)
        servers = stored.get("servers", [])
        invalid = [s for s in servers if not self.is_ready(s)]
        if invalid:
            names = ", ".join(s.get("name", s.get("id", "?")) for s in invalid)
            logger.warning(
                "Ignoring %d server(s) without URL (re-add with name | url | token): %s",
                len(invalid),
                names,
            )
        self._data = {
            "servers": servers,
            "user_selection": {
                str(k): v for k, v in stored.get("user_selection", {}).items()
            },
        }

    @staticmethod
    def is_ready(entry: dict[str, Any]) -> bool:
        return bool(entry.get("url", "").strip() and entry.get("token", "").strip())

    @property
    def ready_servers(self) -> list[dict[str, Any]]:
        return [deepcopy(s) for s in self._data["servers"] if self.is_ready(s)]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(self._data, handle, indent=2)

    @property
    def servers(self) -> list[dict[str, Any]]:
        return list(self._data["servers"])

    def get(self, server_id: str) -> dict[str, Any] | None:
        for server in self._data["servers"]:
            if server["id"] == server_id:
                return deepcopy(server)
        return None

    def get_by_name(self, name: str) -> dict[str, Any] | None:
        needle = name.strip().lower()
        for server in self._data["servers"]:
            if server["name"].lower() == needle:
                return deepcopy(server)
        return None

    def add(self, name: str, url: str, token: str) -> dict[str, Any]:
        normalized_url = url.rstrip("/")
        for server in self._data["servers"]:
            if not self.is_ready(server):
                continue
            if server["url"].rstrip("/") == normalized_url:
                raise ValueError(f"Server URL already registered as {server['name']}")
            if server["name"].lower() == name.strip().lower():
                raise ValueError(f"Server name already in use: {server['name']}")
        entry = {
            "id": uuid.uuid4().hex[:12],
            "name": name.strip(),
            "url": normalized_url,
            "token": token,
        }
        self._data["servers"].append(entry)
        self.save()
        return deepcopy(entry)

    def remove(self, name: str) -> bool:
        needle = name.strip().lower()
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
        if server_id is None:
            self._data["user_selection"].pop(key, None)
        else:
            self._data["user_selection"][key] = server_id
        self.save()

    def get_selection(self, chat_id: int) -> str | None:
        return self._data["user_selection"].get(str(chat_id))

    @staticmethod
    def generate_token() -> str:
        return secrets.token_urlsafe(24)
