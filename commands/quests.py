"""Public daily quest board and single-claim reward buttons."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from database import (
    DailyQuestAlreadyClaimed,
    DailyQuestError,
    Database,
    InvalidAmount,
)


class QuestClaimButton(discord.ui.Button["QuestClaimView"]):
    def __init__(
        self,
        quest_view: "QuestClaimView",
        quest: dict[str, object],
    ) -> None:
        quest_key = str(quest["quest_key"])
        super().__init__(
            label=f"Claim {int(quest['reward']):,} coins",
            style=discord.ButtonStyle.success,
            custom_id=(
                f"thsl:quest:{quest_view.user_id}:"
                f"{quest_view.day_utc}:{quest_key}"
            )[:100],
        )
        self.quest_view = quest_view
        self.quest_key = quest_key

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.quest_view.claim(interaction, self.quest_key)


class QuestClaimView(discord.ui.View):
    def __init__(
        self,
        command: "QuestCommands",
        *,
        user_id: str | int,
        day_utc: str,
        quests: list[dict[str, object]],
    ) -> None:
        super().__init__(timeout=24 * 60 * 60)
        self.command = command
        self.user_id = str(user_id)
        self.day_utc = day_utc
        for quest in quests:
            if quest["completed"] and not quest["claimed"]:
                self.add_item(QuestClaimButton(self, quest))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message(
                "Only the player who owns these quests can claim their rewards.",
                ephemeral=True,
            )
            return False
        return True

    async def claim(
        self,
        interaction: discord.Interaction,
        quest_key: str,
    ) -> None:
        try:
            result = self.command.database.claim_daily_quest(
                self.user_id,
                quest_key,
            )
        except (DailyQuestError, InvalidAmount) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        quests = self.command.database.get_daily_quests(self.user_id)
        user = interaction.user
        embed = self.command._build_embed(
            user,
            quests,
            notice=(
                f"✅ **{result['quest']['description']} claimed!** "
                f"+{int(result['reward']):,} coins."
            ),
        )
        new_view = QuestClaimView(
            self.command,
            user_id=self.user_id,
            day_utc=str(quests[0]["day_utc"]) if quests else self.day_utc,
            quests=quests,
        )
        await interaction.response.edit_message(
            embed=embed,
            view=new_view if new_view.children else None,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class QuestCommands(discord_commands.Cog):
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _build_embed(
        user: discord.abc.User,
        quests: list[dict[str, object]],
        *,
        notice: str | None = None,
    ) -> discord.Embed:
        player = getattr(user, "mention", f"<@{user.id}>")
        description_lines = [f"Player: {player}", "Quests reset at midnight UTC."]
        if notice:
            description_lines.extend(("", notice))
        embed = discord.Embed(
            title="📜 DAILY QUESTS",
            description="\n".join(description_lines),
            color=discord.Color.blurple(),
        )
        for index, quest in enumerate(quests, start=1):
            progress = int(quest["progress"])
            target = int(quest["target"])
            state = "✅ Completed" if quest["completed"] else "In progress"
            if quest["claimed"]:
                state = "✅ Completed · 🎁 Reward claimed"
            embed.add_field(
                name=f"{index}. {quest['description']}",
                value=(
                    f"Progress: **{progress}/{target}** · {state}\n"
                    f"Reward: **{int(quest['reward']):,} coins**"
                ),
                inline=False,
            )
        return embed

    @app_commands.command(
        name="quests",
        description="View your daily quests and claim completed rewards.",
    )
    async def quests(self, interaction: discord.Interaction) -> None:
        quests = self.database.get_daily_quests(interaction.user.id)
        day_utc = str(quests[0]["day_utc"]) if quests else ""
        embed = self._build_embed(interaction.user, quests)
        view = QuestClaimView(
            self,
            user_id=interaction.user.id,
            day_utc=day_utc,
            quests=quests,
        )
        await interaction.response.send_message(
            embed=embed,
            view=view if view.children else None,
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: discord_commands.Bot) -> None:
    await bot.add_cog(QuestCommands(bot.database))
