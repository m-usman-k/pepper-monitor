import json
import os
from typing import Dict


class Storage:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.monitors_path = os.path.join(self.data_dir, "monitors.json")
        self.seen_path = os.path.join(self.data_dir, "seen.json")

    async def ensure(self):
        os.makedirs(self.data_dir, exist_ok=True)
        if not os.path.exists(self.monitors_path):
            with open(self.monitors_path, "w", encoding="utf-8") as f:
                json.dump({}, f)
        if not os.path.exists(self.seen_path):
            with open(self.seen_path, "w", encoding="utf-8") as f:
                json.dump({}, f)

    async def load_monitors(self) -> Dict[str, Dict[str, str]]:
        with open(self.monitors_path, "r", encoding="utf-8") as f:
            return json.load(f)

    async def add_monitor(self, channel_id: int, name: str, url: str):
        data = await self.load_monitors()
        key = str(channel_id)
        data.setdefault(key, {})
        data[key][name] = url
        with open(self.monitors_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    async def remove_monitor(self, channel_id: int, name: str) -> bool:
        data = await self.load_monitors()
        key = str(channel_id)
        if key not in data or name not in data[key]:
            return False
        data[key].pop(name, None)
        if not data[key]:
            data.pop(key, None)
        with open(self.monitors_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True

    async def is_seen(self, composite_key: str) -> bool:
        with open(self.seen_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get(composite_key, False)

    async def mark_seen(self, composite_key: str):
        with open(self.seen_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data[composite_key] = True
        with open(self.seen_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
