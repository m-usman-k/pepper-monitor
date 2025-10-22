import asyncio
import os
import time
from typing import List, Optional

from config import USE_PROXIES, PROXY_ROTATION_SECONDS


class ProxyProvider:
    def __init__(self, path: str = "proxies.txt"):
        self.path = path
        self._proxies: List[str] = []
        self._idx = 0
        self._last_rotate = 0.0
        self._lock = asyncio.Lock()
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            self._proxies = []
            return
        proxies = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                proxies.append(line)
        self._proxies = proxies

    async def get_proxy_url(self) -> Optional[str]:
        if not USE_PROXIES:
            return None
        async with self._lock:
            now = time.time()
            if now - self._last_rotate >= PROXY_ROTATION_SECONDS:
                self._idx = (self._idx + 1) % max(1, len(self._proxies))
                self._last_rotate = now
        if not self._proxies:
            return None
        raw = self._proxies[self._idx % len(self._proxies)]
        # ip:port:user:pass -> http://user:pass@ip:port
        parts = raw.split(":")
        if len(parts) == 4:
            ip, port, user, pwd = parts
            return f"http://{user}:{pwd}@{ip}:{port}"
        elif len(parts) == 2:
            ip, port = parts
            return f"http://{ip}:{port}"
        else:
            return None
