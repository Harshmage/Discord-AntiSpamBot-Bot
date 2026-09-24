import asyncio

from bot.probe import run_probe


def member(uid: str) -> dict:
    return {"member": {"user": {"id": uid, "username": f"user{uid}"}}, "source_invite_code": "abc"}


def test_probe_reports_matches_and_success():
    async def search(guild_id, body):
        signals = body["and_query"].get("safety_signals", {})
        if "unusual_account_activity" in signals:
            return 200, {"members": [member("7")], "total_result_count": 1}
        return 200, {"members": [], "total_result_count": 0}

    report = asyncio.run(run_probe(search, 1))
    assert report.ok
    assert [r.total for r in report.results] == [0, 0, 1]
    assert report.results[2].user_ids == ["7"]
    assert "user7" in report.text


def test_probe_flags_http_errors():
    async def search(guild_id, body):
        return 403, '{"message": "Missing Permissions", "code": 50013}'

    report = asyncio.run(run_probe(search, 1, user_id=5))
    assert not report.ok
    assert all(r.status == 403 and r.total is None for r in report.results)
    assert "Missing Permissions" in report.text
    assert '"5"' in report.text  # scoped to the requested user
