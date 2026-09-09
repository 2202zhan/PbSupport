"""Phase 2: carrying out what the turn decided.

The router's whole job is that nothing falls on the floor: the user is always
answered, and a turn that needed a human actually reaches one.
"""
import pytest

from types import SimpleNamespace

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
        self.markups: list = []

    async def answer(self, text, reply_markup=None):
        self.answered.append(text)
        self.markups.append(reply_markup)


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


class _CallbackMessage(_Message):
    """The bot's own message, which is what a callback carries. Its from_user
    is the bot - the person who tapped comes from the callback itself."""

    def __init__(self):
        super().__init__()
        from types import SimpleNamespace

        self.from_user = SimpleNamespace(id=999, username="printbox_support_bot")
        self.markup_cleared = False

    async def edit_reply_markup(self, reply_markup=None):
        self.markup_cleared = True


class _Callback:
    def __init__(self, data):
        from types import SimpleNamespace

        self.data = data
        self.from_user = SimpleNamespace(id=884013433, username="zhan")
        self.message = _CallbackMessage()
        self.answered = False

    async def answer(self, *_args, **_kwargs):
        self.answered = True


def _turn(monkeypatch, result):
    seen = {}

    async def _run(ctx, *_args, **_kwargs):
        seen["text"] = ctx.user_message
        return result

    monkeypatch.setattr(agent_router, "run_turn", _run)
    return seen


async def test_a_tap_is_read_as_the_words_on_the_button(db, monkeypatch):
    from agent import memory, ui

    conversation = await memory.current_conversation("884013433", "zhan")
    markup = await ui.keyboard(conversation.id, ["только что"])
    seen = _turn(monkeypatch, TurnResult(kind="reply", text="Понял."))

    callback = _Callback(markup.inline_keyboard[0][0].callback_data)
    await agent_router.on_button(callback, None, None)

    assert seen["text"] == "только что"
    assert callback.message.markup_cleared  # can't be tapped twice
    assert callback.message.answered == ["Понял."]


async def test_a_tap_is_attributed_to_the_person_not_the_bot(db, monkeypatch):
    # callback.message.from_user is the bot; using it would file the ticket
    # against the bot's own id.
    from agent import memory, ui

    conversation = await memory.current_conversation("884013433", "zhan")
    markup = await ui.keyboard(conversation.id, [ui.CALL_HUMAN])
    _turn(monkeypatch, TurnResult(kind="escalate", text="Зову.", staff_summary="просит человека"))
    escalated = []
    monkeypatch.setattr(agent_router.notify, "send_plain_escalation", _record(escalated))

    await agent_router.on_button(_Callback(markup.inline_keyboard[0][0].callback_data), None, None)

    ticket = await storage.get_ticket(escalated[0][0])
    assert ticket.telegram_id == "884013433"


async def test_a_stale_button_says_so_instead_of_answering(db, monkeypatch):
    _turn(monkeypatch, TurnResult(kind="reply", text="не должно дойти"))
    callback = _Callback("ab:999999")
    await agent_router.on_button(callback, None, None)
    assert callback.message.answered and "прошлого разговора" in callback.message.answered[0]


async def test_an_answer_carries_the_keyboard_the_agent_drew(db):
    conversation = await _conversation_with("бледно печатает")
    message = _Message()
    await agent_router.deliver(
        None, message, conversation,
        TurnResult(kind="reply", text="Что именно не так?", buttons=["бледно", "полосы"]),
    )
    assert message.markups and [b.text for row in message.markups[0].inline_keyboard for b in row] == [
        "бледно", "полосы", "✋ Позвать человека",
    ]


async def test_no_keyboard_once_a_human_is_on_the_case(db, monkeypatch):
    # Quick answers to the bot would only get in the way of a person replying.
    monkeypatch.setattr(agent_router.notify, "send_plain_escalation", _record([]))
    message = _Message()
    await agent_router.deliver(
        None, message, await _conversation_with("верните деньги"),
        TurnResult(kind="escalate", text="Передал сотруднику.", buttons=["да"], staff_summary="s"),
    )
    assert message.markups == [None]


async def test_what_the_agent_is_waiting_for_is_remembered(db):
    conversation = await _conversation_with("оплатил")
    message = _Message()
    await agent_router.deliver(
        None, message, conversation,
        TurnResult(kind="reply", text="Пришлите чек", expect="file"),
    )
    assert (await storage.get_conversation(conversation)).expecting == "file"


async def test_a_person_on_the_line_is_not_talked_over(db, monkeypatch):
    # Once staff open the conversation, the user's words are theirs. An agent
    # answering over the top of a person mid-sentence is worse than silence.
    relayed = []

    async def _find(_telegram_id):
        return SimpleNamespace(id=7)

    async def _relay(_bot, ticket, message):
        relayed.append((ticket.id, message.text))

    ran = []

    async def _never(*_args, **_kwargs):
        ran.append(True)

    monkeypatch.setattr(agent_router.storage, "find_live_chat_ticket", _find)
    monkeypatch.setattr(agent_router.notify, "relay_user_message", _relay)
    monkeypatch.setattr(agent_router, "run_turn", _never)

    message = _Message("а когда вернёте деньги?")
    await agent_router._handle(None, None, message, message.text)

    assert relayed == [(7, "а когда вернёте деньги?")]
    assert ran == []


async def test_start_does_not_hang_up_on_a_staff_member(db, monkeypatch):
    async def _find(_telegram_id):
        return SimpleNamespace(id=7)

    monkeypatch.setattr(agent_router.storage, "find_live_chat_ticket", _find)
    message = _Message()
    await agent_router.on_start(message)
    assert "сотрудник" in message.answered[0]


async def test_a_recommended_refund_reaches_the_card_as_a_recommendation(db, monkeypatch):
    # The agent may recommend; only guard_rules decides whether the button is
    # even offered, and only a person taps it.
    sent = {}

    async def _send(bot, chat_id, ticket_id, evidence, decision, review=None):
        sent.update(ticket_id=ticket_id, decision=decision, review=review)

    monkeypatch.setattr(agent_router.notify, "send_escalation", _send)

    from agent.types import TurnContext

    ctx = TurnContext("884013433", "zhan", 1, "не вышло")
    ctx.evidence = SimpleNamespace(transaction=None, apparat=None)
    ctx.review = SimpleNamespace(can_refund=False)
    ctx.staff_notes = ["Проверка заказа: транзакция не найдена"]

    message = _Message()
    await agent_router.deliver(
        None, message, await _conversation_with("оплатил"),
        TurnResult(kind="escalate", text="Передал сотруднику.", staff_summary="не вышло",
                   refund_recommended=True, confidence="high", draft_reply="Возврат подтверждён."),
        ctx=ctx,
    )

    assert sent["decision"].action == "recommend_refund"
    assert sent["decision"].confidence == "high"
    assert sent["decision"].draft_reply == "Возврат подтверждён."
    assert "транзакция не найдена" in sent["decision"].staff_summary
    assert sent["review"].can_refund is False  # the rules still say no


async def test_a_found_payment_makes_the_ticket_about_money(db, monkeypatch):
    async def _send(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent_router.notify, "send_escalation", _send)

    from agent.types import TurnContext

    ctx = TurnContext("884013433", "zhan", 1, "не вышло")
    ctx.evidence = SimpleNamespace(
        transaction=SimpleNamespace(id="63192", machine="Аппарат №3"), apparat=None
    )
    ctx.review = SimpleNamespace(can_refund=True)

    message = _Message()
    conversation = await _conversation_with("оплатил, не вышло")
    await agent_router.deliver(
        None, message, conversation,
        TurnResult(kind="escalate", text="Проверяю.", staff_summary="s"),
        ctx=ctx,
    )

    ticket = await storage.get_ticket(1)
    assert ticket.payment_expected == 1
    assert ticket.transaction_id == "63192"
    assert ticket.apparat_name == "Аппарат №3"
