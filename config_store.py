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
    "ram_threshold_percent": None,
    "ram_threshold_gb": 8.0,
    "disk_enabled": True,
    "disk_threshold_percent": 90,
}

DEFAULT_CONFIG: dict[str, Any] = {
    "check_interval_seconds": 30,
    "alert_cooldown_seconds": 300,
    "disk_path": _DEFAULT_DISK_PATH,
    "alert_show_top_processes": True,
    "server_alerts": {},
}

_LEGACY_ALERT_KEYS = (
    "alerts_enabled",
    "cpu_threshold_percent",
    "cpu_sustained_checks",
    "ram_threshold_percent",
    "ram_threshold_gb",
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
        if "ram_threshold_gb" in stored:
            template["ram_threshold_gb"] = stored.pop("ram_threshold_gb")
        if "disk_threshold_percent" in stored:
            template["disk_threshold_percent"] = stored.pop("disk_threshold_percent")

        for key in _LEGACY_ALERT_KEYS:
            stored.pop(key, None)

        stored["server_alerts"] = {"__default__": template}
        return stored

    def load(self) -> None:
        if not self.path.exists():
            self.save()
            return
        with self.path.open(encoding="utf-8") as handle:
            stored = json.load(handle)
        stored = self._migrate_legacy(stored)
        merged = deepcopy(DEFAULT_CONFIG)
        merged.update({k: v for k, v in stored.items() if k != "server_alerts"})
        merged["server_alerts"] = stored.get("server_alerts", {})
        self._data = merged

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
