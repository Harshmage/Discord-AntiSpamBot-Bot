import logging

import discord
from discord.ext import commands, tasks

from .commands import ModCommands
from .config import Config
from .db import Database
from .enforcement import Enforcer
from .signals import SignalClient

log = logging.getLogger(__name__)


class AntiSpamBot(commands.Bot):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.members = True  # privileged: needed for on_member_join
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.config = config
        self.db = Database(config.db_path)
        self.enforcer = Enforcer(self, self.db, SignalClient(self.http), config)

    async def setup_hook(self) -> None:
        await self.db.connect()
        guild = discord.Object(id=self.config.guild_id)
        await self.add_cog(ModCommands(self.db, self.enforcer, self.config), guild=guild)
        await self.tree.sync(guild=guild)
        self.sweep.change_interval(minutes=self.config.sweep_minutes)
        self.sweep.start()

    async def close(self) -> None:
        self.sweep.cancel()
        await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s); dry_run=%s", self.user, self.user.id, self.config.dry_run)

    async def on_member_join(self, member: discord.Member) -> None:
        if member.guild.id == self.config.guild_id:
            await self.enforcer.handle_join(member)

    @tasks.loop(minutes=5)
    async def sweep(self) -> None:
        guild = self.get_guild(self.config.guild_id)
        if guild is not None:
            await self.enforcer.sweep(guild)

    @sweep.before_loop
    async def _before_sweep(self) -> None:
        await self.wait_until_ready()

    @sweep.error
    async def _sweep_error(self, error: BaseException) -> None:
        log.exception("Sweep crashed; restarting", exc_info=error)
        self.sweep.restart()


def main() -> None:
    config = Config.from_env()
    discord.utils.setup_logging()
    AntiSpamBot(config).run(config.token, log_handler=None)
