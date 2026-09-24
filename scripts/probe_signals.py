"""Check that the undocumented members-search endpoint works for this bot and guild.

Takes no action. Prints the raw requests and responses the bot would use, so the
request shape can be checked against what the Members page shows. Moderators can
run the same probe from Discord with /signals probe.

    python scripts/probe_signals.py                 # guild-wide: who is flagged?
    python scripts/probe_signals.py --user 1234     # one member
"""

import argparse
import asyncio
import json
import os
import sys

import aiohttp
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bot.probe import run_probe  # noqa: E402

API = "https://discord.com/api/v10"


async def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", type=int, help="Only check this user ID")
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()

    token = os.environ["DISCORD_TOKEN"]
    guild_id = int(os.environ["GUILD_ID"])
    headers = {"Authorization": f"Bot {token}", "User-Agent": "DiscordBot (antispam-probe, 1.0)"}

    async with aiohttp.ClientSession(headers=headers) as session:

        async def search(gid: int, body: dict) -> tuple[int, object]:
            async with session.post(f"{API}/guilds/{gid}/members-search", json=body) as resp:
                text = await resp.text()
                try:
                    return resp.status, json.loads(text)
                except ValueError:
                    return resp.status, text

        report = await run_probe(search, guild_id, args.user, args.limit)
    print(report.text)


if __name__ == "__main__":
    asyncio.run(main())
