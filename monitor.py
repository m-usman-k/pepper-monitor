import asyncio
import json
import logging
import os
from datetime import datetime
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

    async def formatted_monitor_lines(self) -> list[str]:
        monitors = await self.storage.load_monitors()
        lines: list[str] = []
        for channel_id_str, mapping in monitors.items():
            try:
                cid = int(channel_id_str)
            except Exception:
                cid = None
            channel = self.client.get_channel(cid) if cid else None
            # Use a proper channel mention if available; otherwise, fallback to a readable placeholder
            channel_tag = channel.mention if channel else f"#channel-{channel_id_str}"
            for name, url in mapping.items():
                # Normalize URLs: if it's a subpath (no scheme/host), prefix with the Pepper domain
                disp_url = (url or "").strip()
                if disp_url and not disp_url.lower().startswith(("http://", "https://")):
                    disp_url = disp_url.lstrip("/")
                    disp_url = f"https://www.pepper.pl/{disp_url}"
                # Format: name, full url <channel-mention>
                lines.append(f"{name}, {disp_url} {channel_tag}")
        return lines

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
                    # Fetch a batch to avoid skipping cards when multiple are posted quickly
                    deals = await self.scraper.fetch_latest_batch(url)
                if not deals:
                    await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
                    continue
                # Send unseen in chronological order (oldest first)
                for d in reversed(deals):
                    deal_id = d.unique_id
                    seen_key = f"{channel_id}:{url}:{deal_id}"
                    async with self._io_lock:
                        already = await self.storage.is_seen(seen_key)
                        if not already:
                            await self._send_deal(channel_id, d)
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
        url_for_embed = getattr(deal, "store_url", None) or deal.url
        embed = discord.Embed(
            title=deal.title or "New deal",
            url=url_for_embed,
            color=EMBED_COLOR_HEX,
            description="",
        )
        # Price displayed as old -> new when available
        if getattr(deal, "old_price", None) and deal.price:
            price_text = f"{deal.old_price} zł -> {deal.price} zł"
        else:
            price_text = f"{deal.price} zł" or ""
        if price_text:
            embed.add_field(name="Cena", value=price_text, inline=True)
        if deal.discount:
            d = str(deal.discount).strip()
            # Ensure it looks like '-26%'
            if isinstance(d, str):
                if '%' not in d:
                    d = d + '%'
                if not (d.startswith('-') or d.startswith('−') or d.startswith('+')):
                    d = '-' + d
            embed.add_field(name="Rabat", value=d, inline=True)
        if deal.store:
            embed.add_field(name="Sklep", value=deal.store, inline=True)
        if deal.code:
            embed.add_field(name="Kod", value=f"`{deal.code}`", inline=False)

        print(deal.image)
        if deal.image:
            # Debug print for image URL used in embed
            try:
                print(f"[pepper-monitor] embed image url -> {deal.image}")
            except Exception:
                pass
            logger.info("Embed image URL set: %s", deal.image)
            embed.set_image(url=deal.image)
        guild = channel.guild
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        footer_name = f"{guild.name} • {now}" if guild else now
        embed.set_footer(text=footer_name)
        msg = await channel.send(embed=embed)
        try:
            await msg.add_reaction("✅")
            await msg.add_reaction("❌")
        except Exception:
            pass
