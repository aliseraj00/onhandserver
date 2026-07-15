import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

_DEFAULT_DISK_PATH = os.getenv("DISK_PATH", "/")

SERVER_ALERT_DEFAULT: dict[str, Any] = {
    "enabled": True,
    "cpu_enabled": True,
    "cpu_threshold_percent": 90,
    "cpu_sustained_checks": 3,
    "ram_enabled": True,
    "ram_threshold_percent": 90,
    "disk_enabled": True,
    "disk_threshold_percent": 90,
}

BACKUP_DEFAULT: dict[str, Any] = {
    "enabled": False,
    "path": "",
    "interval_minutes": 60,
    "notify_chat_id": None,
}

DEFAULT_CONFIG: dict[str, Any] = {
    "check_interval_seconds": 30,
    "alert_cooldown_seconds": 300,
    "disk_path": _DEFAULT_DISK_PATH,
    "alert_show_top_processes": True,
    "server_alerts": {},
    "backup": deepcopy(BACKUP_DEFAULT),
}

_LEGACY_ALERT_KEYS = (
    "alerts_enabled",
    "cpu_threshold_percent",
    "cpu_sustained_checks",
    "ram_threshold_percent",
    "disk_threshold_percent",
)


class ConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data = deepcopy(DEFAULT_CONFIG)
        self.load()

    def _migrate_legacy(self, stored: dict[str, Any]) -> dict[str, Any]:
        if "server_alerts" in stored:
            return stored
        if not any(key in stored for key in _LEGACY_ALERT_KEYS):
            stored["server_alerts"] = {}
            return stored

        template = deepcopy(SERVER_ALERT_DEFAULT)
        if "alerts_enabled" in stored:
            template["enabled"] = bool(stored.pop("alerts_enabled"))
        if "cpu_threshold_percent" in stored:
            template["cpu_threshold_percent"] = stored.pop("cpu_threshold_percent")
        if "cpu_sustained_checks" in stored:
            template["cpu_sustained_checks"] = stored.pop("cpu_sustained_checks")
        if "ram_threshold_percent" in stored:
            template["ram_threshold_percent"] = stored.pop("ram_threshold_percent")
        stored.pop("ram_threshold_gb", None)
        if "disk_threshold_percent" in stored:
            template["disk_threshold_percent"] = stored.pop("disk_threshold_percent")

        for key in _LEGACY_ALERT_KEYS:
            stored.pop(key, None)

        stored["server_alerts"] = {"__default__": template}
        return stored

    def _normalize_server_alerts(self) -> None:
        server_alerts = self._data.get("server_alerts", {})
        for entry in server_alerts.values():
            entry.pop("ram_threshold_gb", None)
            if entry.get("ram_threshold_percent") is None:
                entry["ram_threshold_percent"] = SERVER_ALERT_DEFAULT[
                    "ram_threshold_percent"
                ]

    def load(self) -> None:
        if not self.path.exists():
            self.save()
            return
        with self.path.open(encoding="utf-8") as handle:
            stored = json.load(handle)
        had_legacy = "server_alerts" not in stored and any(
            key in stored for key in _LEGACY_ALERT_KEYS
        )
        stored = self._migrate_legacy(stored)
        merged = deepcopy(DEFAULT_CONFIG)
        merged.update(
            {
                k: v
                for k, v in stored.items()
                if k not in ("server_alerts", "backup")
            }
        )
        merged["server_alerts"] = stored.get("server_alerts", {})
        backup = deepcopy(BACKUP_DEFAULT)
        backup.update(stored.get("backup") or {})
        merged["backup"] = backup
        self._data = merged
        self._normalize_server_alerts()
        if had_legacy:
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(self._data, handle, indent=2)

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def get_server_alerts(self, server_id: str) -> dict[str, Any]:
        merged = deepcopy(SERVER_ALERT_DEFAULT)
        defaults = self._data.get("server_alerts", {}).get("__default__")
        if defaults:
            merged.update(defaults)
        specific = self._data.get("server_alerts", {}).get(server_id)
        if specific:
            merged.update(specific)
        merged.pop("ram_threshold_gb", None)
        if merged.get("ram_threshold_percent") is None:
            merged["ram_threshold_percent"] = SERVER_ALERT_DEFAULT[
                "ram_threshold_percent"
            ]
        return merged

    def update_server_alerts(self, server_id: str, **changes: Any) -> None:
        if "server_alerts" not in self._data:
            self._data["server_alerts"] = {}
        current = deepcopy(self._data["server_alerts"].get(server_id, {}))
        current.update(changes)
        self._data["server_alerts"][server_id] = current
        self.save()

    def update_global(self, **changes: Any) -> None:
        self._data.update(changes)
        self.save()

    def get_backup(self) -> dict[str, Any]:
        merged = deepcopy(BACKUP_DEFAULT)
        stored = self._data.get("backup") or {}
        merged.update(stored)
        return merged

    def update_backup(self, **changes: Any) -> None:
        current = self.get_backup()
        current.update(changes)
        self._data["backup"] = current
        self.save()
