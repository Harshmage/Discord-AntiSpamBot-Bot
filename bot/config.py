"""Runtime configuration, loaded from environment variables (or a .env file)."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _optional_int(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    return int(value)


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Config:
    token: str
    guild_id: int
    mod_channel_id: int
    mod_role_id: int | None = None
    sweep_minutes: float = 5.0
    ban_delete_seconds: int = 86400
    dry_run: bool = True
    db_path: str = "/data/bot.db"
    join_check_delay: float = 5.0

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        return cls(
            token=_required("DISCORD_TOKEN"),
            guild_id=int(_required("GUILD_ID")),
            mod_channel_id=int(_required("MOD_CHANNEL_ID")),
            mod_role_id=_optional_int(os.getenv("MOD_ROLE_ID")),
            sweep_minutes=float(os.getenv("SWEEP_MINUTES", "5")),
            # Discord caps message deletion on ban at 7 days.
            ban_delete_seconds=max(0, min(int(os.getenv("BAN_DELETE_SECONDS", "86400")), 604800)),
            dry_run=_bool(os.getenv("DRY_RUN"), True),
            db_path=os.getenv("DB_PATH", "/data/bot.db"),
            join_check_delay=float(os.getenv("JOIN_CHECK_DELAY", "5")),
        )
