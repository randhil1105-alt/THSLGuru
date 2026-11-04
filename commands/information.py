"""Help and bot information slash commands."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands as discord_commands


class InformationCommands(discord_commands.Cog):
    @app_commands.command(name="help", description="Show the bot command guide.")
    async def help(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="THSL Guru Commands",
            description="Collect hockey player cards and manage your coin balance.",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Cards",
            value=(
                "`/player name` · Look up a player\n"
                "`/card name` · Show the full card\n"
                "`/inventory` · View cards you own\n"
                "`/collection` · Browse the catalog\n"
                "`/team` · View the lineup image and substitutes\n"
                "`/set position player` · Assign or clear any of five slots\n"
                "`/formation formation` · Save a tactical style"
            ),
            inline=False,
        )
        embed.add_field(
            name="Coins and packs",
            value=(
                "`/claim` · Claim 5,000 coins every 24 hours\n"
                "`/balance` · Check your balance\n"
                "`/leaderboard` · See top balances\n"
                "`/packs` · View pack contents, odds, prices, and buy buttons\n"
                "`/goldpack` · Confirm a 5,000,000-coin GOLD purchase\n"
                "`/totwpack` · Confirm a 20,000,000-coin TOTW purchase\n"
                "`/sell player quantity` · Sell cards at listed value"
            ),
            inline=False,
        )
        embed.add_field(
            name="Administrators",
            value=(
                "`/addplayer` · Add a card to the catalog\n"
                "`/setbalance` · Set a member's balance\n"
                "`/grantcoins` · Grant coins\n"
                "`/grantcard` · Grant card copies"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="botinfo", description="Show bot and economy information.")
    async def botinfo(self, interaction: discord.Interaction) -> None:
        client = interaction.client
        embed = discord.Embed(
            title="THSL Guru",
            description="Discord hockey card collection and coin economy.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Discord.py", value=discord.__version__, inline=True)
        embed.add_field(
            name="Player cards",
            value=str(len(client.database.get_players())),
            inline=True,
        )
        embed.add_field(name="Storage", value="Persistent SQLite", inline=True)
        await interaction.response.send_message(embed=embed)


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(InformationCommands())
