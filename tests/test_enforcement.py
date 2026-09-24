import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.config import Config
from bot.db import Database
from bot.enforcement import Enforcer
from bot.signals import UNUSUAL_ACCOUNT, UNUSUAL_DM, Lookup, SignalLookupError

GUILD_ID = 1
USER_ID = 42


def make_config(**overrides) -> Config:
    values = dict(token="t", guild_id=GUILD_ID, mod_channel_id=2, dry_run=False, db_path=":memory:", join_check_delay=0)
    values.update(overrides)
    return Config(**values)


def make_member() -> MagicMock:
    member = MagicMock()
    member.id = USER_ID
    member.bot = False
    member.guild.id = GUILD_ID
    member.guild.kick = AsyncMock()
    member.guild.ban = AsyncMock()
    return member


def run(coro_fn, **config_overrides):
    """Run coro_fn(enforcer, db, member) with a fresh in-memory DB."""

    async def inner():
        db = Database(":memory:")
        await db.connect()
        enforcer = Enforcer(MagicMock(), db, MagicMock(), make_config(**config_overrides))
        enforcer._blocker = MagicMock(return_value=None)
        enforcer._report = AsyncMock()
        enforcer._send = AsyncMock()
        try:
            return await coro_fn(enforcer, db, make_member())
        finally:
            await db.close()

    return asyncio.run(inner())


def test_first_offense_is_kicked_and_recorded():
    async def scenario(enforcer, db, member):
        assert await enforcer.evaluate(member, "join", frozenset({UNUSUAL_DM})) == "kick"
        member.guild.kick.assert_awaited_once()
        member.guild.ban.assert_not_awaited()
        assert await db.has_prior_kick(GUILD_ID, USER_ID)

    run(scenario)


def test_rejoin_while_flagged_is_banned():
    async def scenario(enforcer, db, member):
        await db.record_action(GUILD_ID, USER_ID, "kick", [UNUSUAL_ACCOUNT], "join", dry_run=False)
        assert await enforcer.evaluate(member, "join", frozenset({UNUSUAL_ACCOUNT})) == "ban"
        member.guild.ban.assert_awaited_once()
        member.guild.kick.assert_not_awaited()

    run(scenario)


def test_whitelisted_user_is_skipped():
    async def scenario(enforcer, db, member):
        await db.add_whitelist(GUILD_ID, USER_ID, added_by=7, reason="known good")
        assert await enforcer.evaluate(member, "sweep", frozenset({UNUSUAL_DM, UNUSUAL_ACCOUNT})) is None
        member.guild.kick.assert_not_awaited()
        enforcer._report.assert_not_awaited()

    run(scenario)


def test_no_signals_means_no_action():
    async def scenario(enforcer, db, member):
        assert await enforcer.evaluate(member, "join", frozenset()) is None
        member.guild.kick.assert_not_awaited()

    run(scenario)


def test_lookup_failure_takes_no_action_and_alerts_after_repeats():
    async def scenario(enforcer, db, member):
        enforcer.signals.lookup = AsyncMock(side_effect=SignalLookupError("HTTP 403"))
        for _ in range(3):
            assert await enforcer.handle_join(member) is None
        member.guild.kick.assert_not_awaited()
        member.guild.ban.assert_not_awaited()
        enforcer._send.assert_awaited_once()  # a single outage alert

    run(scenario)


def test_join_uses_lookup_result():
    async def scenario(enforcer, db, member):
        enforcer.signals.lookup = AsyncMock(
            return_value=Lookup(indexed=True, signals=frozenset({UNUSUAL_DM}), hit=None)
        )
        assert await enforcer.handle_join(member) == "kick"

    run(scenario)


def test_unindexed_join_is_left_to_sweep():
    async def scenario(enforcer, db, member):
        enforcer.signals.lookup = AsyncMock(return_value=Lookup(indexed=False, signals=frozenset(), hit=None))
        assert await enforcer.handle_join(member) is None
        member.guild.kick.assert_not_awaited()

    run(scenario)


def test_dry_run_reports_once_and_never_acts():
    async def scenario(enforcer, db, member):
        signals = frozenset({UNUSUAL_DM})
        assert await enforcer.evaluate(member, "sweep", signals) == "would_kick"
        assert await enforcer.evaluate(member, "sweep", signals) is None
        member.guild.kick.assert_not_awaited()
        enforcer._report.assert_awaited_once()
        # Dry-run records must not count as a real kick for rejoin detection.
        assert not await db.has_prior_kick(GUILD_ID, USER_ID)

    run(scenario, dry_run=True)


def test_blocked_member_is_reported_once_without_action():
    async def scenario(enforcer, db, member):
        enforcer._blocker = MagicMock(return_value="Member's highest role is at or above the bot's highest role.")
        assert await enforcer.evaluate(member, "sweep", frozenset({UNUSUAL_DM})) == "blocked"
        assert await enforcer.evaluate(member, "sweep", frozenset({UNUSUAL_DM})) == "blocked"
        member.guild.kick.assert_not_awaited()
        enforcer._report.assert_awaited_once()

    run(scenario)


@pytest.mark.parametrize("signal", [UNUSUAL_DM, UNUSUAL_ACCOUNT])
def test_signal_filter_shapes(signal):
    from bot.signals import signal_filter

    f = signal_filter(signal)
    assert len(f) == 1
