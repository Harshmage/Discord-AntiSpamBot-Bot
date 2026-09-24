"""Decides and carries out kicks/bans for members carrying tracked Signals."""

import asyncio
import logging

import discord

from .config import Config
from .db import Database
from .signals import (
    JOIN_SOURCE_TYPES,
    SIGNAL_DESCRIPTIONS,
    SIGNAL_NAMES,
    SIGNALS,
    Lookup,
    SearchHit,
    SignalClient,
    SignalLookupError,
)

log = logging.getLogger(__name__)

OUTAGE_THRESHOLD = 3
JOIN_INDEX_RETRIES = 3


class Enforcer:
    def __init__(self, bot: discord.Client, db: Database, signals: SignalClient, config: Config):
        self.bot = bot
        self.db = db
        self.signals = signals
        self.config = config
        self._in_progress: set[int] = set()
        # Keys already reported where no real action is taken (dry-run, blocked),
        # so the sweep doesn't repost them every few minutes.
        self._reported: set[tuple[int, str]] = set()
        self._failures = 0
        self._outage_alerted = False

    # Signal lookup with outage tracking

    async def _lookup_ok(self) -> None:
        self._failures = 0
        if self._outage_alerted:
            self._outage_alerted = False
            await self._send(discord.Embed(
                title="✅ Signals lookup recovered",
                description="The members-search endpoint is responding again. Enforcement has resumed.",
                colour=discord.Colour.green(),
            ))

    async def _lookup_failed(self, error: Exception) -> None:
        self._failures += 1
        log.warning("Signals lookup failed (%s in a row): %s", self._failures, error)
        if self._failures >= OUTAGE_THRESHOLD and not self._outage_alerted:
            self._outage_alerted = True
            await self._send(discord.Embed(
                title="⚠️ Signals lookup unavailable",
                description=(
                    "The bot could not read member Signals from Discord "
                    f"{self._failures} times in a row, so **no automatic actions are being taken**.\n"
                    f"Last error: `{str(error)[:300]}`\n\n"
                    "The members-search endpoint is undocumented and may have changed, "
                    "or the bot may be missing the **Manage Server** permission."
                ),
                colour=discord.Colour.gold(),
            ))

    async def lookup(self, guild_id: int, user_id: int) -> Lookup | None:
        try:
            result = await self.signals.lookup(guild_id, user_id)
        except SignalLookupError as e:
            await self._lookup_failed(e)
            return None
        await self._lookup_ok()
        return result

    # Triggers

    async def handle_join(self, member: discord.Member) -> str | None:
        if member.bot or await self.db.is_whitelisted(member.guild.id, member.id):
            return None
        # The search index can lag a few seconds behind the join.
        for _ in range(JOIN_INDEX_RETRIES):
            await asyncio.sleep(self.config.join_check_delay)
            result = await self.lookup(member.guild.id, member.id)
            if result is None:
                return None  # lookup failed; the sweep will retry later
            if result.indexed:
                return await self.evaluate(member, "join", result.signals, result.hit)
        log.info("%s (%s) not in the search index yet; leaving it to the sweep", member, member.id)
        return None

    async def sweep(self, guild: discord.Guild) -> None:
        try:
            flagged = await self.signals.flagged(guild.id)
        except SignalLookupError as e:
            await self._lookup_failed(e)
            return
        await self._lookup_ok()
        for user_id, (signals, hit) in flagged.items():
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.NotFound:
                    continue  # already gone
            await self.evaluate(member, "sweep", signals, hit)

    # Decision

    def _blocker(self, member: discord.Member, action: str) -> str | None:
        """Why the bot can't act on this member, if anything prevents it."""
        guild = member.guild
        me = guild.me
        if member.id == guild.owner_id:
            return "Member is the server owner."
        if member.guild_permissions.administrator:
            return "Member has the Administrator permission."
        if member.top_role >= me.top_role:
            return "Member's highest role is at or above the bot's highest role."
        perms = me.guild_permissions
        if action == "kick" and not perms.kick_members:
            return "Bot is missing the Kick Members permission."
        if action == "ban" and not perms.ban_members:
            return "Bot is missing the Ban Members permission."
        return None

    async def evaluate(
        self, member: discord.Member, trigger: str, signals: frozenset[str], hit: SearchHit | None = None
    ) -> str | None:
        """Kick or ban `member` for carrying `signals`. Returns what was done, if anything."""
        if not signals or member.bot or member.id in self._in_progress:
            return None
        self._in_progress.add(member.id)
        try:
            guild = member.guild
            if await self.db.is_whitelisted(guild.id, member.id):
                return None

            ordered = [s for s in SIGNALS if s in signals]
            rejoin = await self.db.has_prior_kick(guild.id, member.id)
            action = "ban" if rejoin else "kick"

            blocker = self._blocker(member, action)
            if blocker:
                if (member.id, "blocked") not in self._reported:
                    self._reported.add((member.id, "blocked"))
                    await self._report(member, "blocked", ordered, trigger, hit, rejoin, note=blocker)
                return "blocked"

            if self.config.dry_run:
                if (member.id, action) in self._reported:
                    return None
                self._reported.add((member.id, action))
                await self.db.record_action(guild.id, member.id, action, ordered, trigger, dry_run=True)
                await self._report(member, action, ordered, trigger, hit, rejoin, dry_run=True)
                return f"would_{action}"

            reason = "Anti-spam: Discord signal(s): " + ", ".join(SIGNAL_NAMES[s] for s in ordered)
            if rejoin:
                reason += " (rejoined while still flagged)"
            try:
                if action == "ban":
                    await guild.ban(member, reason=reason, delete_message_seconds=self.config.ban_delete_seconds)
                else:
                    await guild.kick(member, reason=reason)
            except discord.HTTPException as e:
                log.exception("Failed to %s %s (%s)", action, member, member.id)
                await self._report(member, "failed", ordered, trigger, hit, rejoin,
                                   note=f"Attempted {action} failed: HTTP {e.status} {e.text}"[:1000])
                return "failed"

            await self.db.record_action(guild.id, member.id, action, ordered, trigger, dry_run=False)
            log.info("%s %s (%s) for %s via %s", action, member, member.id, ordered, trigger)
            await self._report(member, action, ordered, trigger, hit, rejoin)
            return action
        finally:
            self._in_progress.discard(member.id)

    # Mod-channel reporting

    async def _send(self, embed: discord.Embed, file: discord.File | None = None) -> discord.Message | None:
        channel = self.bot.get_channel(self.config.mod_channel_id)
        try:
            if channel is None:
                channel = await self.bot.fetch_channel(self.config.mod_channel_id)
            kwargs = {"file": file} if file else {}
            return await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none(), **kwargs)
        except discord.HTTPException:
            log.exception("Could not post to the mod channel %s", self.config.mod_channel_id)
            return None

    async def _report(
        self,
        member: discord.Member,
        action: str,
        signals: list[str],
        trigger: str,
        hit: SearchHit | None,
        rejoin: bool,
        dry_run: bool = False,
        note: str | None = None,
    ) -> None:
        titles = {
            "kick": ("👢 Kicked", discord.Colour.orange()),
            "ban": ("🔨 Banned (rejoined while still flagged)", discord.Colour.red()),
            "blocked": ("⚠️ Flagged member, no action possible", discord.Colour.gold()),
            "failed": ("❌ Action failed", discord.Colour.dark_red()),
        }
        title, colour = titles[action]
        if dry_run:
            title = f"🧪 DRY RUN: would {'ban (rejoin)' if action == 'ban' else action}"
            colour = discord.Colour.light_grey()

        embed = discord.Embed(title=title, colour=colour, timestamp=discord.utils.utcnow())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="User", value=f"{member.mention}\n`{member.name}`", inline=True)
        embed.add_field(name="User ID", value=f"`{member.id}`", inline=True)
        embed.add_field(name="Display name", value=discord.utils.escape_markdown(member.display_name), inline=True)
        embed.add_field(
            name="Account created",
            value=f"{discord.utils.format_dt(member.created_at, 'f')} ({discord.utils.format_dt(member.created_at, 'R')})",
            inline=True,
        )
        if member.joined_at:
            embed.add_field(
                name="Joined server",
                value=f"{discord.utils.format_dt(member.joined_at, 'f')} ({discord.utils.format_dt(member.joined_at, 'R')})",
                inline=True,
            )
        if hit is not None:
            parts = []
            if hit.join_source_type is not None:
                parts.append(JOIN_SOURCE_TYPES.get(hit.join_source_type, f"Type {hit.join_source_type}"))
            if hit.source_invite_code:
                parts.append(f"invite `{hit.source_invite_code}`")
            if hit.inviter_id:
                parts.append(f"invited by <@{hit.inviter_id}> (`{hit.inviter_id}`)")
            if parts:
                embed.add_field(name="Joined via", value=", ".join(parts), inline=True)
        embed.add_field(
            name="Signal(s) identified",
            value="\n".join(f"• **{SIGNAL_NAMES[s]}**: {SIGNAL_DESCRIPTIONS[s]}" for s in signals),
            inline=False,
        )
        embed.add_field(name="Detected on", value="Member join" if trigger == "join" else "Periodic sweep", inline=True)
        embed.add_field(name="Previously kicked", value="Yes" if rejoin else "No", inline=True)
        if note:
            embed.add_field(name="Note", value=note, inline=False)
        embed.set_footer(text=f"Use /whitelist add to exempt this user · ID {member.id}")
        await self._send(embed)
