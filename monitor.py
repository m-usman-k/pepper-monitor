import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import discord

from config import (
    DATA_DIR,
    REFRESH_INTERVAL_SECONDS,
    EMBED_COLOR_HEX,
)
from scraper import PepperScraper
from storage import Storage

logger = logging.getLogger(__name__)


@dataclass
class ListInfo:
    current_channel: Dict[str, str]
    total_channels: int
    total_monitors: int


class MonitorManager:
    def __init__(self, client: discord.Client):
        self.client = client
        self.storage = Storage(DATA_DIR)
        self.scraper = PepperScraper()
        self._tasks: Dict[Tuple[int, str], asyncio.Task] = {}
        self._locks: Dict[Tuple[int, str], asyncio.Lock] = {}
        self._io_lock = asyncio.Lock()

    async def initialize(self):
        await self.storage.ensure()
        # Start tasks for persisted monitors
        monitors = await self.storage.load_monitors()
        for channel_id_str, mapping in monitors.items():
            for name, url in mapping.items():
                await self._start_monitor(int(channel_id_str), name, url)
        logger.info("Initialized %d monitors", len(self._tasks))

    async def add_monitor(self, channel_id: int, name: str, url: str):
        key = (channel_id, name)
        if key in self._tasks:
            raise ValueError("Monitor with this name already exists in this channel")
        # Persist
        async with self._io_lock:
            await self.storage.add_monitor(channel_id, name, url)
        # Start task
        await self._start_monitor(channel_id, name, url)

    async def remove_monitor(self, channel_id: int, name: str) -> bool:
        key = (channel_id, name)
        task = self._tasks.pop(key, None)
        if task:
            task.cancel()
        async with self._io_lock:
            removed = await self.storage.remove_monitor(channel_id, name)
        return removed

    async def list_monitors(self, channel_id: int) -> ListInfo:
        monitors = await self.storage.load_monitors()
        channel_map = monitors.get(str(channel_id), {})
        total_channels = len(monitors)
        total_monitors = sum(len(m) for m in monitors.values())
        return ListInfo(current_channel=channel_map, total_channels=total_channels, total_monitors=total_monitors)

    async def _start_monitor(self, channel_id: int, name: str, url: str):
        key = (channel_id, name)
        if key in self._tasks:
            return
        lock = asyncio.Lock()
        self._locks[key] = lock
        task = asyncio.create_task(self._monitor_loop(channel_id, name, url, lock), name=f"monitor:{channel_id}:{name}")
        self._tasks[key] = task

    async def _monitor_loop(self, channel_id: int, name: str, url: str, lock: asyncio.Lock):
        await self.client.wait_until_ready()
        channel = self.client.get_channel(channel_id)
        if channel is None:
            logger.warning("Channel %s not found; monitor '%s' will idle until available", channel_id, name)
        while True:
            try:
                async with lock:
                    latest = await self.scraper.fetch_latest(url)
                if latest is None:
                    await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
                    continue
                deal_id = latest.unique_id
                seen_key = f"{channel_id}:{url}:{deal_id}"
                async with self._io_lock:
                    already = await self.storage.is_seen(seen_key)
                    if not already:
                        await self._send_deal(channel_id, latest)
                        await self.storage.mark_seen(seen_key)
            except asyncio.CancelledError:
                logger.info("Monitor cancelled: %s - %s", channel_id, name)
                break
            except Exception:
                logger.exception("Error in monitor '%s' for channel %s", name, channel_id)
            finally:
                await asyncio.sleep(REFRESH_INTERVAL_SECONDS)

    async def _send_deal(self, channel_id: int, deal):
        channel = self.client.get_channel(channel_id)
        if channel is None:
            logger.warning("Cannot send deal; channel %s not found", channel_id)
            return
        embed = discord.Embed(
            title=deal.title or "New deal",
            url=deal.url,
            color=EMBED_COLOR_HEX,
            description=deal.description or "",
        )
        if deal.price:
            embed.add_field(name="Price", value=deal.price, inline=True)
        if deal.discount:
            embed.add_field(name="Discount", value=deal.discount, inline=True)
        if deal.store:
            embed.add_field(name="Store", value=deal.store, inline=True)
        if deal.code:
            embed.add_field(name="Code", value=f"`{deal.code}`", inline=False)
        if deal.image:
            embed.set_image(url=deal.image)
        embed.set_footer(text="pepper.pl monitor")
        await channel.send(embed=embed)
