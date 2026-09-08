"""Phase 1: what the agent remembers between messages.

The menu bot had no memory - every message restarted the flow from a state
machine. These tests pin the three things that replace it: one live
conversation per person, one turn at a time, and a history that stays bounded.
"""
import asyncio
import json

import pytest

import storage
from agent import memory


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage.settings, "support_bot_db_path", str(tmp_path / "t.sqlite3"))
    storage.init_db()


async def test_a_second_message_lands_in_the_same_conversation(db):
    first = await memory.current_conversation("884013433", "zhan")
    await memory.remember_user_message(first.id, "не распечатал")
    again = await memory.current_conversation("884013433", "zhan")
    assert again.id == first.id


async def test_a_conversation_that_went_quiet_starts_a_fresh_one(db, monkeypatch):
    # Tomorrow's unrelated question should not inherit yesterday's context.
    old = await memory.current_conversation("884013433")
    monkeypatch.setattr(memory.settings, "agent_conversation_ttl_minutes", 0)
    new = await memory.current_conversation("884013433")
    assert new.id != old.id
    assert (await storage.get_conversation(old.id)).status == "closed"


async def test_one_user_never_has_two_live_conversations(db, monkeypatch):
    await memory.current_conversation("884013433")
    monkeypatch.setattr(memory.settings, "agent_conversation_ttl_minutes", 0)
    await memory.current_conversation("884013433")
    # Only one row is still open, however generous the TTL.
    assert (await storage.active_conversation("884013433", 60 * 24 * 365)) is not None
    opened = [c for c in await _all_conversations() if c["status"] == "open"]
    assert len(opened) == 1


async def _all_conversations():
    import sqlite3

    conn = sqlite3.connect(storage.settings.support_bot_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM conversations")]
    finally:
        conn.close()


async def test_two_users_do_not_share_a_conversation(db):
    mine = await memory.current_conversation("1")
    theirs = await memory.current_conversation("2")
    assert mine.id != theirs.id


async def test_history_replays_the_exchange_in_api_shape(db):
    conv = await memory.current_conversation("1")
    await memory.remember_user_message(conv.id, "бледно печатает")
    await memory.remember_assistant_message(
        conv.id, None, tool_calls=[{"id": "c1", "type": "function",
                                    "function": {"name": "check_apparat", "arguments": "{}"}}]
    )
    await memory.remember_tool_result(conv.id, "check_apparat", "c1", {"вердикт": "всё в норме"})
    await memory.remember_assistant_message(conv.id, "Проверил — расходники в порядке.")

    replay = await memory.history(conv.id)
    assert [m["role"] for m in replay] == ["user", "assistant", "tool", "assistant"]
    assert replay[1]["tool_calls"][0]["function"]["name"] == "check_apparat"
    assert replay[2]["tool_call_id"] == "c1"
    assert "в норме" in replay[2]["content"]


async def test_a_short_history_is_left_alone(db):
    conv = await memory.current_conversation("1")
    for i in range(4):
        await memory.remember_user_message(conv.id, f"сообщение {i}")

    async def _never(_messages):
        raise AssertionError("should not have summarised a short history")

    assert await memory.compact_if_needed(conv.id, _never) is False


async def test_a_long_history_is_folded_but_keeps_the_recent_tail(db, monkeypatch):
    monkeypatch.setattr(memory.settings, "agent_history_max_messages", 10)
    conv = await memory.current_conversation("1")
    for i in range(20):
        await memory.remember_user_message(conv.id, f"сообщение {i}")

    folded = {}

    async def _summarise(messages):
        folded["count"] = len(messages)
        return "юзер жаловался на печать"

    assert await memory.compact_if_needed(conv.id, _summarise) is True
    replay = await memory.history(conv.id)
    assert replay[0]["role"] == "system" and "жаловался" in replay[0]["content"]
    # The most recent exchange survives verbatim - that is what the next turn needs.
    assert replay[-1]["content"] == "сообщение 19"
    assert len(replay) < 20
    assert folded["count"] == 20 - memory._KEEP_VERBATIM


async def test_compaction_never_orphans_a_tool_result(db, monkeypatch):
    # A tool result whose call was folded away is an orphan the API rejects.
    monkeypatch.setattr(memory.settings, "agent_history_max_messages", 4)
    monkeypatch.setattr(memory, "_KEEP_VERBATIM", 1)
    conv = await memory.current_conversation("1")
    await memory.remember_user_message(conv.id, "не печатает")
    for i in range(4):
        await memory.remember_assistant_message(
            conv.id, None, tool_calls=[{"id": f"c{i}", "type": "function",
                                        "function": {"name": "t", "arguments": "{}"}}]
        )
        await memory.remember_tool_result(conv.id, "t", f"c{i}", "ok")

    await memory.compact_if_needed(conv.id, lambda _m: _echo("свернул"))
    replay = await memory.history(conv.id)
    for i, m in enumerate(replay):
        if m["role"] == "tool":
            assert replay[i - 1]["role"] == "assistant" and replay[i - 1].get("tool_calls")


async def _echo(text):
    return text


async def test_a_broken_summariser_leaves_the_history_intact(db, monkeypatch):
    # Losing the ability to compact must not lose the conversation.
    monkeypatch.setattr(memory.settings, "agent_history_max_messages", 4)
    conv = await memory.current_conversation("1")
    for i in range(10):
        await memory.remember_user_message(conv.id, f"m{i}")

    async def _boom(_messages):
        raise RuntimeError("model down")

    assert await memory.compact_if_needed(conv.id, _boom) is False
    assert len(await memory.history(conv.id)) == 10


async def test_two_messages_from_one_person_do_not_run_at_once(db):
    # "не печатает" then "аппарат 3" a second later must be two turns in order,
    # not two turns reading the same history.
    order = []

    async def _turn(name, delay):
        async with memory.one_turn_at_a_time("884013433"):
            order.append(f"{name}:start")
            await asyncio.sleep(delay)
            order.append(f"{name}:end")

    await asyncio.gather(_turn("first", 0.02), _turn("second", 0))
    assert order in (
        ["first:start", "first:end", "second:start", "second:end"],
        ["second:start", "second:end", "first:start", "first:end"],
    )


async def test_different_people_are_not_queued_behind_each_other(db):
    running = []

    async def _turn(user):
        async with memory.one_turn_at_a_time(user):
            running.append(user)
            await asyncio.sleep(0.02)
            assert len(running) == 2  # both inside their locks at once

    await asyncio.gather(_turn("1"), _turn("2"))


async def test_locks_do_not_pile_up_after_the_turn(db):
    async with memory.one_turn_at_a_time("1"):
        pass
    assert memory._locks == {} and memory._waiting == {}


async def test_a_tool_result_is_stored_as_text_whatever_it_was(db):
    conv = await memory.current_conversation("1")
    await memory.remember_tool_result(conv.id, "check_apparat", "c1", {"бумага": "есть"})
    (message,) = await storage.conversation_messages(conv.id)
    assert json.loads(message.content) == {"бумага": "есть"}


async def test_the_ttl_is_measured_against_the_clock_the_rows_use(db):
    # The rows carry UTC; computing the cutoff from naive Asia/Almaty
    # wall-clock instead compared two different clocks and quietly cut a
    # six-hour TTL down to one.
    import sqlite3
    from datetime import datetime, timedelta, timezone

    conv = await memory.current_conversation("884013433")
    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    conn = sqlite3.connect(storage.settings.support_bot_db_path)
    conn.execute("UPDATE conversations SET last_at = ? WHERE id = ?", (two_hours_ago, conv.id))
    conn.commit()
    conn.close()

    assert (await memory.current_conversation("884013433")).id == conv.id
