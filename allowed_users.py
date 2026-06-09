import json
from pathlib import Path


class AllowedUsersStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._chat_ids: set[int] = set()
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._chat_ids = set()
            return
        with self.path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        self._chat_ids = {int(chat_id) for chat_id in data.get("chat_ids", [])}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump({"chat_ids": sorted(self._chat_ids)}, handle, indent=2)

    @property
    def chat_ids(self) -> set[int]:
        return set(self._chat_ids)

    def add(self, chat_id: int) -> bool:
        if chat_id in self._chat_ids:
            return False
        self._chat_ids.add(chat_id)
        self.save()
        return True

    def remove(self, chat_id: int) -> bool:
        if chat_id not in self._chat_ids:
            return False
        self._chat_ids.remove(chat_id)
        self.save()
        return True
