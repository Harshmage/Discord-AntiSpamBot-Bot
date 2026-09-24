"""Diagnostic probe of the members-search endpoint. Takes no action.

Shared by scripts/probe_signals.py (raw aiohttp) and the /signals probe command
(the bot's own HTTP client), so both send exactly the requests the bot uses.
"""

import json
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .signals import SIGNAL_NAMES, SIGNALS, signal_filter, user_query

# (guild_id, body) -> (http_status, parsed JSON or raw text)
SearchFn = Callable[[int, dict], Awaitable[tuple[int, object]]]


@dataclass
class QueryResult:
    label: str
    status: int
    total: int | None = None  # None when the response had no member list
    user_ids: list[str] = field(default_factory=list)


@dataclass
class ProbeReport:
    results: list[QueryResult]
    text: str

    @property
    def ok(self) -> bool:
        return all(r.status == 200 and r.total is not None for r in self.results)


async def run_probe(search: SearchFn, guild_id: int, user_id: int | None = None, limit: int = 25) -> ProbeReport:
    scope = user_query(user_id) if user_id else {}
    queries = [("Unfiltered (is the endpoint reachable?)", scope, min(limit, 5))]
    queries += [
        (SIGNAL_NAMES[s], {**scope, "safety_signals": signal_filter(s)}, limit) for s in SIGNALS
    ]

    lines: list[str] = []
    results: list[QueryResult] = []
    for label, and_query, lim in queries:
        body = {"limit": lim, "and_query": and_query}
        lines += [f"=== {label} ===", f"POST /guilds/{guild_id}/members-search", json.dumps(body, indent=2)]
        status, data = await search(guild_id, body)
        lines.append(f"-> HTTP {status}")
        result = QueryResult(label, status)
        if isinstance(data, dict) and "members" in data:
            result.total = data.get("total_result_count", len(data["members"]))
            lines.append(f"total_result_count={result.total}")
            for m in data["members"]:
                user = m.get("member", {}).get("user", {})
                result.user_ids.append(str(user.get("id")))
                lines.append(
                    f"  {user.get('id')}  {user.get('username')}  invite={m.get('source_invite_code')} "
                    f"join_source_type={m.get('join_source_type')} inviter={m.get('inviter_id')}"
                )
            if data["members"]:
                lines.append("First raw result:\n" + json.dumps(data["members"][0], indent=2)[:3000])
        elif isinstance(data, (dict, list)):
            lines.append(json.dumps(data, indent=2)[:2000])
        else:
            lines.append(str(data)[:2000])
        lines.append("")
        results.append(result)

    return ProbeReport(results, "\n".join(lines))
