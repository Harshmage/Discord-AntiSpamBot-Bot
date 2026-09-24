"""SQLite storage for the whitelist and the enforcement history.

Discord keeps no record of kicks, so the `actions` table is how the bot knows a
flagged member is rejoining after a previous kick.
"""

import os
import sqlite3
import time
from dataclasses import dataclass

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS whitelist (
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    added_by  INTEGER NOT NULL,
    reason    TEXT,
    added_at  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS actions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    action    TEXT    NOT NULL,
    signals   TEXT    NOT NULL,
    trigger   TEXT    NOT NULL,
    dry_run   INTEGER NOT NULL DEFAULT 0,
    at        INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS actions_by_user ON actions (guild_id, user_id);
"""


@dataclass(frozen=True)
class WhitelistEntry:
    user_id: int
    added_by: int
    reason: str | None
    added_at: int


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        try:
            self._conn = await aiosqlite.connect(self.path)
        except sqlite3.OperationalError as e:
            raise RuntimeError(
                f"Cannot open database at {self.path!r} ({e}). "
                f"Check that its folder exists and is writable by UID {os.getuid()}."
            ) from e
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    # Whitelist

    async def is_whitelisted(self, guild_id: int, user_id: int) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM whitelist WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ) as cur:
            return await cur.fetchone() is not None

    async def add_whitelist(self, guild_id: int, user_id: int, added_by: int, reason: str | None) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO whitelist (guild_id, user_id, added_by, reason, added_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, added_by, reason, int(time.time())),
        )
        await self.conn.commit()

    async def remove_whitelist(self, guild_id: int, user_id: int) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM whitelist WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def list_whitelist(self, guild_id: int) -> list[WhitelistEntry]:
        async with self.conn.execute(
            "SELECT user_id, added_by, reason, added_at FROM whitelist WHERE guild_id = ? ORDER BY added_at",
            (guild_id,),
        ) as cur:
            return [WhitelistEntry(*row) for row in await cur.fetchall()]

    # Action history

    async def record_action(
        self, guild_id: int, user_id: int, action: str, signals: list[str], trigger: str, dry_run: bool
    ) -> None:
        await self.conn.execute(
            "INSERT INTO actions (guild_id, user_id, action, signals, trigger, dry_run, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, action, ",".join(signals), trigger, int(dry_run), int(time.time())),
        )
        await self.conn.commit()

    async def has_prior_kick(self, guild_id: int, user_id: int) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM actions WHERE guild_id = ? AND user_id = ? AND action = 'kick' AND dry_run = 0 LIMIT 1",
            (guild_id, user_id),
        ) as cur:
            return await cur.fetchone() is not None
