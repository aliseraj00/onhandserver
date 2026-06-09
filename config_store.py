import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

_DEFAULT_DISK_PATH = os.getenv("DISK_PATH", "/")

DEFAULT_CONFIG: dict[str, Any] = {
    "alerts_enabled": True,
    "check_interval_seconds": 30,
    "alert_cooldown_seconds": 300,
    "cpu_threshold_percent": 90,
    "cpu_sustained_checks": 3,
    "ram_threshold_percent": None,
    "ram_threshold_gb": 8.0,
    "disk_threshold_percent": 90,
    "disk_path": _DEFAULT_DISK_PATH,
}


class ConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data = deepcopy(DEFAULT_CONFIG)
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.save()
            return
        with self.path.open(encoding="utf-8") as handle:
            stored = json.load(handle)
        merged = deepcopy(DEFAULT_CONFIG)
        merged.update(stored)
        self._data = merged

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(self._data, handle, indent=2)

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def update(self, **changes: Any) -> None:
        self._data.update(changes)
        self.save()
