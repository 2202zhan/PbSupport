"""Phase 0: every agent turn leaves a row, so the new path can be compared
with the old one instead of guessed about."""
import asyncio
import sqlite3

import pytest

import storage


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage.settings, "support_bot_db_path", str(tmp_path / "t.sqlite3"))
    storage.init_db()
    return tmp_path / "t.sqlite3"


async def test_a_turn_is_recorded_with_its_tools_and_cost(db):
    await storage.record_agent_turn(
        telegram_id="884013433",
        outcome="reply",
        user_message="не распечатал",
        reply="Проверил ваш заказ…",
        tool_calls=["find_my_orders", "investigate_order"],
        model_calls=2,
        latency_ms=4200,
        prompt_tokens=1800,
        completion_tokens=140,
    )
    (row,) = await storage.recent_agent_turns()
    assert row["outcome"] == "reply"
    assert row["tool_call_count"] == 2
    assert "investigate_order" in row["tool_calls"]
    assert row["latency_ms"] == 4200


async def test_a_failed_turn_is_recorded_too(db):
    # A turn that fell over is the most interesting one to look at later.
    await storage.record_agent_turn(
        telegram_id="884013433", outcome="failed", error="timeout", model_calls=1
    )
    (row,) = await storage.recent_agent_turns()
    assert row["outcome"] == "failed" and row["error"] == "timeout"


async def test_recent_turns_come_back_newest_first(db):
    for i in range(3):
        await storage.record_agent_turn(telegram_id="1", outcome="reply", reply=f"#{i}")
    rows = await storage.recent_agent_turns(limit=2)
    assert [r["reply"] for r in rows] == ["#2", "#1"]


async def test_bookkeeping_never_breaks_the_conversation(db, monkeypatch):
    # Losing a metric is cheaper than losing the user's turn, so a broken write
    # is swallowed rather than raised into the handler.
    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(storage, "_record_agent_turn_sync", _boom)
    await storage.record_agent_turn(telegram_id="1", outcome="reply")


def test_the_table_survives_an_older_database(tmp_path, monkeypatch):
    # Real databases predate this table; init_db has to add it in place.
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tickets (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(storage.settings, "support_bot_db_path", str(path))
    storage.init_db()
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        storage.record_agent_turn(telegram_id="1", outcome="reply")
    )
