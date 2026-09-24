"""Moderator slash commands: /whitelist and /signals."""

import io

import discord
from discord import app_commands
from discord.ext import commands

from .config import Config
from .db import Database
from .enforcement import Enforcer
from .probe import run_probe
from .signals import SIGNAL_DESCRIPTIONS, SIGNAL_NAMES, SIGNALS

MOD_PERMISSIONS = discord.Permissions(kick_members=True)


class ModCommands(commands.Cog):
    whitelist = app_commands.Group(
        name="whitelist",
        description="Exempt users from automatic anti-spam action",
        default_permissions=MOD_PERMISSIONS,
        guild_only=True,
    )
    signals = app_commands.Group(
        name="signals",
        description="Inspect Discord safety signals",
        default_permissions=MOD_PERMISSIONS,
        guild_only=True,
    )

    def __init__(self, db: Database, enforcer: Enforcer, config: Config):
        self.db = db
        self.enforcer = enforcer
        self.config = config

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # default_permissions only sets the initial visibility; server admins can
        # override it, so enforce the moderator requirement here too.
        user = interaction.user
        if not isinstance(user, discord.Member):
            return False
        if user.guild_permissions.administrator or user.guild_permissions.kick_members:
            return True
        return self.config.mod_role_id is not None and user.get_role(self.config.mod_role_id) is not None

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        message = (
            "You must be a moderator to use this command."
            if isinstance(error, app_commands.CheckFailure)
            else f"Something went wrong: {error}"
        )
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def _log(self, text: str) -> None:
        await self.enforcer._send(discord.Embed(description=text, colour=discord.Colour.blurple()))

    @whitelist.command(name="add", description="Exempt a user (by picker or ID) from automatic kicks/bans")
    @app_commands.describe(user="The user to exempt; they don't need to be in the server", reason="Why they are exempt")
    async def whitelist_add(self, interaction: discord.Interaction, user: discord.User, reason: str | None = None):
        await self.db.add_whitelist(interaction.guild_id, user.id, interaction.user.id, reason)
        await interaction.response.send_message(f"✅ {user.mention} (`{user.id}`) is now whitelisted.", ephemeral=True)
        await self._log(
            f"📝 {interaction.user.mention} whitelisted {user.mention} (`{user.name}`, `{user.id}`)"
            + (f"\nReason: {reason}" if reason else "")
        )

    @whitelist.command(name="remove", description="Remove a user's anti-spam exemption")
    @app_commands.describe(user="The user to remove from the whitelist")
    async def whitelist_remove(self, interaction: discord.Interaction, user: discord.User):
        removed = await self.db.remove_whitelist(interaction.guild_id, user.id)
        if not removed:
            await interaction.response.send_message(f"{user.mention} was not whitelisted.", ephemeral=True)
            return
        await interaction.response.send_message(f"🗑️ {user.mention} removed from the whitelist.", ephemeral=True)
        await self._log(f"📝 {interaction.user.mention} removed {user.mention} (`{user.id}`) from the whitelist")

    @whitelist.command(name="list", description="Show all whitelisted users")
    async def whitelist_list(self, interaction: discord.Interaction):
        entries = await self.db.list_whitelist(interaction.guild_id)
        if not entries:
            await interaction.response.send_message("The whitelist is empty.", ephemeral=True)
            return
        lines = [
            f"<@{e.user_id}> (`{e.user_id}`), added by <@{e.added_by}> <t:{e.added_at}:d>"
            + (f": {e.reason}" if e.reason else "")
            for e in entries
        ]
        text = ""
        for i, line in enumerate(lines):
            if len(text) + len(line) > 3900:
                text += f"\n…and {len(lines) - i} more"
                break
            text += line + "\n"
        embed = discord.Embed(title=f"Whitelist ({len(entries)})", description=text, colour=discord.Colour.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @signals.command(name="check", description="Show a member's tracked safety signals without taking action")
    @app_commands.describe(member="The member to check")
    async def signals_check(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.enforcer.lookup(interaction.guild_id, member.id)
        whitelisted = await self.db.is_whitelisted(interaction.guild_id, member.id)
        if result is None:
            text = "❌ Could not read signals from Discord (see logs / mod channel)."
        elif not result.indexed:
            text = "⏳ This member isn't in Discord's search index yet. Try again shortly."
        elif not result.signals:
            text = "✅ No tracked signals."
        else:
            text = "\n".join(
                f"🚩 **{SIGNAL_NAMES[s]}**: {SIGNAL_DESCRIPTIONS[s]}" for s in SIGNALS if s in result.signals
            )
        if whitelisted:
            text += "\n\n📝 This member is whitelisted."
        await interaction.followup.send(f"{member.mention} (`{member.id}`)\n{text}", ephemeral=True)

    @signals.command(name="probe", description="Test the Signals endpoint and post the raw results to the mod channel")
    @app_commands.describe(
        member="Only probe this member (default: the whole server)",
        limit="Maximum members listed per signal (default 25)",
    )
    async def signals_probe(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        limit: app_commands.Range[int, 1, 1000] = 25,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        report = await run_probe(
            self.enforcer.signals.raw_search, interaction.guild_id, member.id if member else None, limit
        )

        embed = discord.Embed(
            title="🔎 Signals probe " + ("succeeded" if report.ok else "found a problem"),
            description=f"Run by {interaction.user.mention}"
            + (f" for {member.mention} (`{member.id}`)" if member else " for the whole server")
            + ". No action was taken.",
            colour=discord.Colour.green() if report.ok else discord.Colour.red(),
            timestamp=discord.utils.utcnow(),
        )
        for r in report.results:
            if r.total is None:
                value = f"HTTP {r.status}: no member list returned"
            else:
                value = f"HTTP {r.status}: **{r.total}** match(es)"
                if r.user_ids:
                    shown = ", ".join(f"<@{uid}>" for uid in r.user_ids[:15])
                    more = f" …+{len(r.user_ids) - 15}" if len(r.user_ids) > 15 else ""
                    value += f"\n{shown}{more}"
            embed.add_field(name=r.label, value=value[:1024], inline=False)
        embed.set_footer(text="Full requests and responses are attached. Compare with Server Settings → Members.")

        file = discord.File(io.BytesIO(report.text.encode()), filename="signals-probe.txt")
        message = await self.enforcer._send(embed, file=file)
        if message is None:
            # Mod channel unreachable: hand the results to the moderator directly.
            file = discord.File(io.BytesIO(report.text.encode()), filename="signals-probe.txt")
            await interaction.followup.send(
                "⚠️ Couldn't post to the mod channel, so here are the results.", embed=embed, file=file, ephemeral=True
            )
            return
        await interaction.followup.send(
            f"{'✅' if report.ok else '⚠️'} Probe results posted: {message.jump_url}", ephemeral=True
        )
