"""Lookup of Discord's member safety Signals.

Signals are not part of the regular member object or the GUILD_MEMBER_ADD event.
The only bot-accessible source is the Member Search V2 endpoint that powers the
Members page (POST /guilds/{guild_id}/members-search). It is undocumented, so
every failure raises SignalLookupError and callers must never act on a failed
lookup.
"""

import asyncio
import logging
import time
from dataclasses import dataclass

import discord
from discord.http import HTTPClient, Route

log = logging.getLogger(__name__)

UNUSUAL_DM = "unusual_dm_activity"
UNUSUAL_ACCOUNT = "unusual_account_activity"
SIGNALS = (UNUSUAL_DM, UNUSUAL_ACCOUNT)

SIGNAL_NAMES = {
    UNUSUAL_DM: "Unusual DM Activity",
    UNUSUAL_ACCOUNT: "Unusual Account Activity",
}
SIGNAL_DESCRIPTIONS = {
    UNUSUAL_DM: "Excessive DMs to non-friend server members",
    UNUSUAL_ACCOUNT: "Engaged in suspected spam activity",
}

JOIN_SOURCE_TYPES = {
    0: "Unspecified",
    1: "Bot",
    2: "Integration",
    3: "Server Discovery",
    4: "Student Hub",
    5: "Invite",
    6: "Vanity URL",
    7: "Manual member verification",
}

MAX_LIMIT = 1000


class SignalLookupError(Exception):
    pass


def signal_filter(signal: str) -> dict:
    """The `safety_signals` query that matches members carrying `signal`."""
    if signal == UNUSUAL_DM:
        # The signal is stored as an expiry timestamp (ms); it is active while in the future.
        return {"unusual_dm_activity_until": {"range": {"gte": int(time.time() * 1000)}}}
    if signal == UNUSUAL_ACCOUNT:
        return {"unusual_account_activity": True}
    raise ValueError(f"Unknown signal: {signal}")


def user_query(user_id: int) -> dict:
    return {"user_id": {"or_query": [str(user_id)]}}


@dataclass(frozen=True)
class SearchHit:
    user_id: int
    source_invite_code: str | None
    join_source_type: int | None
    inviter_id: int | None

    @classmethod
    def from_payload(cls, payload: dict) -> "SearchHit":
        user = payload.get("member", {}).get("user", {})
        inviter = payload.get("inviter_id")
        return cls(
            user_id=int(user["id"]),
            source_invite_code=payload.get("source_invite_code"),
            join_source_type=payload.get("join_source_type"),
            inviter_id=int(inviter) if inviter else None,
        )


@dataclass(frozen=True)
class Lookup:
    indexed: bool  # False when the search index doesn't know the member yet
    signals: frozenset[str]
    hit: SearchHit | None


class SignalClient:
    def __init__(self, http: HTTPClient):
        self._http = http

    async def raw_search(self, guild_id: int, body: dict) -> tuple[int, object]:
        """One unparsed request, for diagnostics. Returns (status, response body)."""
        route = Route("POST", "/guilds/{guild_id}/members-search", guild_id=guild_id)
        try:
            data = await self._http.request(route, json=body)
        except discord.HTTPException as e:
            return e.status, e.text
        # discord.py hides the 2xx status; 202 responses carry no member list.
        return (200 if isinstance(data, dict) and "members" in data else 202), data

    async def _search(self, guild_id: int, and_query: dict, limit: int) -> list[SearchHit]:
        body = {"limit": limit, "and_query": and_query}
        route = Route("POST", "/guilds/{guild_id}/members-search", guild_id=guild_id)
        for _ in range(3):
            try:
                data = await self._http.request(route, json=body)
            except discord.HTTPException as e:
                raise SignalLookupError(f"members-search returned HTTP {e.status}: {e.text}") from e
            if isinstance(data, dict) and "members" in data:
                hits = [SearchHit.from_payload(m) for m in data["members"]]
                total = data.get("total_result_count", len(hits))
                if total > len(hits):
                    log.warning("members-search matched %s members but returned only %s", total, len(hits))
                return hits
            # HTTP 202: the guild's search index is still being built.
            retry_after = data.get("retry_after", 5) if isinstance(data, dict) else 5
            await asyncio.sleep(min(float(retry_after), 15))
        raise SignalLookupError("members-search index is not ready")

    async def lookup(self, guild_id: int, user_id: int) -> Lookup:
        """Which of the tracked signals one member currently carries."""
        base = await self._search(guild_id, user_query(user_id), 1)
        if not base:
            return Lookup(indexed=False, signals=frozenset(), hit=None)
        found = set()
        for signal in SIGNALS:
            query = {**user_query(user_id), "safety_signals": signal_filter(signal)}
            if await self._search(guild_id, query, 1):
                found.add(signal)
        return Lookup(indexed=True, signals=frozenset(found), hit=base[0])

    async def flagged(self, guild_id: int) -> dict[int, tuple[frozenset[str], SearchHit]]:
        """Every member of the guild carrying at least one tracked signal."""
        result: dict[int, tuple[set[str], SearchHit]] = {}
        for signal in SIGNALS:
            for hit in await self._search(guild_id, {"safety_signals": signal_filter(signal)}, MAX_LIMIT):
                result.setdefault(hit.user_id, (set(), hit))[0].add(signal)
        return {uid: (frozenset(sigs), hit) for uid, (sigs, hit) in result.items()}
