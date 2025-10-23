import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands

from config import BOT_TOKEN, OWNER_ID, EMBED_COLOR_HEX
from monitor import MonitorManager


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pepper-bot")


class PepperBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.monitor: Optional[MonitorManager] = None

    async def setup_hook(self) -> None:
        self.monitor = MonitorManager(self)
        await self.monitor.initialize()
        # Sync commands globally
        await self.tree.sync()
        logger.info("Slash commands synced")

    async def on_ready(self):
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)


bot = PepperBot()


def owner_only(interaction: discord.Interaction) -> bool:
    return interaction.user and interaction.user.id == OWNER_ID


@app_commands.checks.check(lambda i: owner_only(i))
@bot.tree.command(name="help", description="Show available commands for Pepper monitor bot")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Pepper Monitor Help",
        description=(
            "Commands:\n"
            "/alert add [name] [link] – enable monitoring of a subpage in this channel\n"
            "/alert remove [name] – disable monitoring by name in this channel\n"
            "/alert list – list monitored links for this channel and globally"
        ),
        color=EMBED_COLOR_HEX,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


class AlertGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="alert", description="Manage Pepper monitors")

    @app_commands.checks.check(lambda i: owner_only(i))
    @app_commands.command(name="add", description="Enable monitoring of a subpage in this channel")
    @app_commands.describe(name="Name to identify this monitor", link="Pepper subpage URL to monitor")
    async def add(self, interaction: discord.Interaction, name: str, link: str):
        assert bot.monitor is not None
        await interaction.response.defer(ephemeral=True)
        try:
            await bot.monitor.add_monitor(channel_id=interaction.channel_id, name=name, url=link)
            await interaction.followup.send(f"Added monitor '{name}' for {link} in this channel.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed to add monitor: {e}", ephemeral=True)

    @app_commands.checks.check(lambda i: owner_only(i))
    @app_commands.command(name="remove", description="Disable monitoring in this channel by name")
    @app_commands.describe(name="Name used when adding the monitor")
    async def remove(self, interaction: discord.Interaction, name: str):
        assert bot.monitor is not None
        await interaction.response.defer(ephemeral=True)
        try:
            removed = await bot.monitor.remove_monitor(channel_id=interaction.channel_id, name=name)
            if removed:
                await interaction.followup.send(f"Removed monitor '{name}' in this channel.", ephemeral=True)
            else:
                await interaction.followup.send(f"No monitor named '{name}' in this channel.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed to remove monitor: {e}", ephemeral=True)

    @app_commands.checks.check(lambda i: owner_only(i))
    @app_commands.command(name="list", description="List monitors for this channel and global totals")
    async def list_cmd(self, interaction: discord.Interaction):
        assert bot.monitor is not None
        await interaction.response.defer(ephemeral=True)
        data = await bot.monitor.list_monitors(channel_id=interaction.channel_id)
        lines = await bot.monitor.formatted_monitor_lines()
        desc = "\n".join(lines) if lines else "No monitors added."
        embed = discord.Embed(title="Alert list", description=desc, color=EMBED_COLOR_HEX)
        embed.add_field(name="Total channels", value=str(data.total_channels))
        embed.add_field(name="Total monitors", value=str(data.total_monitors))
        await interaction.followup.send(embed=embed, ephemeral=True)


bot.tree.add_command(AlertGroup())


def main():
    bot.run(BOT_TOKEN)


if __name__ == "__main__":
    main()
