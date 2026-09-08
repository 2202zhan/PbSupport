"""Phase 2: carrying out what the turn decided.

The router's whole job is that nothing falls on the floor: the user is always
answered, and a turn that needed a human actually reaches one.
"""
import pytest

import storage
from agent import router as agent_router
from agent.types import TurnResult


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage.settings, "support_bot_db_path", str(tmp_path / "t.sqlite3"))
    storage.init_db()


class _Message:
    def __init__(self, text="не распечатал"):
        from types import SimpleNamespace

        self.text = text
        self.from_user = SimpleNamespace(id=884013433, username="zhan")
        self.answered: list[str] = []

    async def answer(self, text, reply_markup=None):
        self.answered.append(text)


async def test_an_answer_is_sent_and_nobody_is_bothered(db, monkeypatch):
    escalated = []
    monkeypatch.setattr(agent_router.notify, "send_plain_escalation", _record(escalated))

    message = _Message()
    await agent_router.deliver(None, message, 1, TurnResult(kind="reply", text="Работаем с 8 до 19."))
    assert message.answered == ["Работаем с 8 до 19."]
    assert escalated == []


def _record(bucket):
    async def _send(bot, chat_id, ticket_id, decision):
        bucket.append((ticket_id, decision))

    return _send


async def test_an_escalation_opens_a_ticket_and_a_card(db, monkeypatch):
    escalated = []
    monkeypatch.setattr(agent_router.notify, "send_plain_escalation", _record(escalated))

    conversation = await _conversation_with("оплатил, ничего не вышло")
    message = _Message()
    await agent_router.deliver(
        None, message, conversation,
        TurnResult(kind="escalate", text="Передал сотруднику.",
                   staff_summary="Юзер оплатил, распечатка не вышла.", reason="money"),
    )
    assert message.answered == ["Передал сотруднику."]
    (ticket_id, decision) = escalated[0]
    ticket = await storage.get_ticket(ticket_id)
    assert ticket.problem_type == "agent"
    assert "оплатил" in ticket.raw_text
    assert decision.staff_summary == "Юзер оплатил, распечатка не вышла."


async def _conversation_with(*messages):
    from agent import memory

    conversation = await memory.current_conversation("884013433", "zhan")
    for m in messages:
        await memory.remember_user_message(conversation.id, m)
    return conversation.id


async def test_a_broken_turn_still_says_something(db, monkeypatch):
    # The runtime supplies the text; if it ever doesn't, the user must not be
    # left staring at nothing.
    monkeypatch.setattr(agent_router.notify, "send_plain_escalation", _record([]))
    message = _Message()
    await agent_router.deliver(None, message, await _conversation_with("привет"),
                               TurnResult(kind="failed", staff_summary="сломалось"))
    assert message.answered and message.answered[0]


async def test_a_failing_staff_channel_does_not_break_the_reply(db, monkeypatch):
    # The user has already been told a human is coming; a broken staff chat is
    # logged, not raised into the handler.
    async def _boom(*_args, **_kwargs):
        raise RuntimeError("staff chat unavailable")

    monkeypatch.setattr(agent_router.notify, "send_plain_escalation", _boom)
    message = _Message()
    await agent_router.deliver(None, message, await _conversation_with("привет"),
                               TurnResult(kind="escalate", text="Зову коллегу.", staff_summary="s"))
    assert message.answered == ["Зову коллегу."]


async def test_start_begins_a_fresh_conversation(db):
    from agent import memory

    old = await memory.current_conversation("884013433", "zhan")
    await memory.remember_user_message(old.id, "вчерашняя проблема")

    message = _Message()
    await agent_router.on_start(message)

    new = await memory.current_conversation("884013433", "zhan")
    assert new.id != old.id
    assert await memory.history(new.id) == []
    assert message.answered and "PrintBox" in message.answered[0]
