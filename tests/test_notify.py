from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import csat
import notify
import storage
from guard_rules import Decision


def _fake_callback(data: str, message_text: str = "🆘 Заявка #1\n...") -> AsyncMock:
    callback = AsyncMock()
    callback.data = data
    callback.from_user = SimpleNamespace(full_name="Staff Name")
    callback.message.message_id = 999
    callback.message.text = message_text
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    return callback


def _fake_ticket(
    status: str = "escalated",
    transaction_id: str | None = "tx-1",
    draft_reply: str | None = None,
    forum_topic_id: int | None = None,
) -> storage.TicketRecord:
    return storage.TicketRecord(
        id=1, telegram_id="123", username="user", contact=None,
        problem_type="not_printed", apparat_name="Аппарат №1", raw_text="не печатает",
        transaction_id=transaction_id, status=status, draft_reply=draft_reply,
        forum_topic_id=forum_topic_id, payment_expected=1, live_chat=0, created_at="2026-06-29T12:00:00",
    )


async def test_already_resolved_ticket_blocks_second_confirm():
    # The buttons stay visible on an already-resolved escalation message -
    # a second tap must not refund a second time.
    callback = _fake_callback(f"{notify._CONFIRM_PREFIX}1")
    bot = AsyncMock()
    api = AsyncMock()

    with patch.object(storage, "get_ticket", AsyncMock(return_value=_fake_ticket(status="resolved_refund"))), \
         patch.object(storage, "resolve_escalation", AsyncMock()) as resolve_mock:
        await notify.handle_escalation_decision(callback, bot, api)

    api.refund_transaction.assert_not_called()
    resolve_mock.assert_not_called()
    callback.answer.assert_called_once()
    assert callback.answer.call_args.kwargs.get("show_alert") is True
    callback.message.edit_text.assert_not_called()


async def test_already_rejected_ticket_blocks_second_reject():
    callback = _fake_callback(f"{notify._REJECT_PREFIX}1")
    bot = AsyncMock()
    api = AsyncMock()

    with patch.object(storage, "get_ticket", AsyncMock(return_value=_fake_ticket(status="resolved_rejected"))), \
         patch.object(storage, "resolve_escalation", AsyncMock()) as resolve_mock:
        await notify.handle_escalation_decision(callback, bot, api)

    resolve_mock.assert_not_called()
    callback.message.edit_text.assert_not_called()


async def test_first_confirm_refunds_and_removes_buttons():
    callback = _fake_callback(f"{notify._CONFIRM_PREFIX}1")
    bot = AsyncMock()
    api = AsyncMock()

    with patch.object(storage, "get_ticket", AsyncMock(return_value=_fake_ticket(status="escalated"))), \
         patch.object(storage, "resolve_escalation", AsyncMock()), \
         patch.object(storage, "record_decision", AsyncMock()), \
         patch.object(storage, "set_ticket_status", AsyncMock()) as set_status_mock, \
         patch.object(csat, "send_poll", AsyncMock()):
        await notify.handle_escalation_decision(callback, bot, api)

    api.refund_transaction.assert_called_once_with("tx-1")
    set_status_mock.assert_called_once_with(1, "resolved_refund")
    callback.message.edit_text.assert_called_once()
    _, kwargs = callback.message.edit_text.call_args
    # Refund/reject are gone so nobody taps them twice, but staff keep a way to
    # write to the user afterwards.
    callbacks = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert callbacks == [f"{notify._REPLY_PREFIX}1"]


async def test_confirm_sends_the_ai_draft_reply_to_the_user():
    callback = _fake_callback(f"{notify._CONFIRM_PREFIX}1")
    bot = AsyncMock()
    api = AsyncMock()
    ticket = _fake_ticket(draft_reply="Проверил — принтер не получил файл. Возврат 70 ₸ оформлен.")

    with patch.object(storage, "get_ticket", AsyncMock(return_value=ticket)), \
         patch.object(storage, "resolve_escalation", AsyncMock()), \
         patch.object(storage, "record_decision", AsyncMock()), \
         patch.object(storage, "set_ticket_status", AsyncMock()), \
         patch.object(csat, "send_poll", AsyncMock()):
        await notify.handle_escalation_decision(callback, bot, api)

    bot.send_message.assert_called_once()
    args, _ = bot.send_message.call_args
    assert args[1] == ticket.draft_reply


async def test_confirm_without_draft_falls_back_to_the_standard_reply():
    callback = _fake_callback(f"{notify._CONFIRM_PREFIX}1")
    bot = AsyncMock()
    api = AsyncMock()

    with patch.object(storage, "get_ticket", AsyncMock(return_value=_fake_ticket())), \
         patch.object(storage, "resolve_escalation", AsyncMock()), \
         patch.object(storage, "record_decision", AsyncMock()), \
         patch.object(storage, "set_ticket_status", AsyncMock()), \
         patch.object(csat, "send_poll", AsyncMock()):
        await notify.handle_escalation_decision(callback, bot, api)

    args, _ = bot.send_message.call_args
    assert args[1] == notify._FALLBACK_REFUND_REPLY


async def test_confirm_records_the_refund_against_its_transaction():
    # was_already_refunded() looks the refund up by transaction_id inside the
    # evidence blob - without it, a second refund on the same order is invisible.
    callback = _fake_callback(f"{notify._CONFIRM_PREFIX}1")
    bot = AsyncMock()
    api = AsyncMock()

    with patch.object(storage, "get_ticket", AsyncMock(return_value=_fake_ticket())), \
         patch.object(storage, "resolve_escalation", AsyncMock()), \
         patch.object(storage, "record_decision", AsyncMock()) as record_mock, \
         patch.object(storage, "set_ticket_status", AsyncMock()), \
         patch.object(csat, "send_poll", AsyncMock()):
        await notify.handle_escalation_decision(callback, bot, api)

    args, _ = record_mock.call_args
    assert args[1] == {"transaction_id": "tx-1"}
    assert args[-1] == "refund_confirmed"


def test_blocked_case_offers_no_refund_button():
    keyboard = notify._escalation_keyboard(1, can_refund=False)
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert not any(c.startswith(notify._CONFIRM_PREFIX) for c in callbacks)
    # Asking for a receipt and replying still make sense on a blocked case -
    # that's often exactly how the blocker gets resolved.
    assert any(c.startswith(notify._ASK_RECEIPT_PREFIX) for c in callbacks)
    assert any(c.startswith(notify._REPLY_PREFIX) for c in callbacks)


def test_clean_case_offers_all_four_actions():
    keyboard = notify._escalation_keyboard(1, can_refund=True)
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert callbacks == [
        f"{notify._CONFIRM_PREFIX}1",
        f"{notify._REJECT_PREFIX}1",
        f"{notify._ASK_RECEIPT_PREFIX}1",
        f"{notify._REPLY_PREFIX}1",
    ]


def test_topic_title_names_the_case():
    title = notify._topic_title(31, "not_printed", "Аппарат №1")
    assert title == "#31 · не печатает · Аппарат №1"


def test_topic_title_stays_within_telegram_limit():
    title = notify._topic_title(31, "not_printed", "А" * 300)
    assert len(title) <= 128


async def test_ask_receipt_writes_to_the_user_and_marks_the_ticket():
    callback = _fake_callback(f"{notify._ASK_RECEIPT_PREFIX}1")
    bot = AsyncMock()

    with patch.object(storage, "get_ticket", AsyncMock(return_value=_fake_ticket(forum_topic_id=42))), \
         patch.object(storage, "set_ticket_status", AsyncMock()) as status_mock:
        await notify.handle_ask_receipt(callback, bot)

    user_message = bot.send_message.call_args_list[0]
    assert user_message.args[0] == 123  # the user, not the staff chat
    status_mock.assert_called_once_with(1, "awaiting_receipt")


async def test_ask_receipt_does_not_mark_the_ticket_if_the_user_is_unreachable():
    # Blocked bot, deleted account - marking it "awaiting_receipt" would leave
    # the ticket waiting for something that was never asked for.
    callback = _fake_callback(f"{notify._ASK_RECEIPT_PREFIX}1")
    bot = AsyncMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("bot was blocked"))

    with patch.object(storage, "get_ticket", AsyncMock(return_value=_fake_ticket())), \
         patch.object(storage, "set_ticket_status", AsyncMock()) as status_mock:
        await notify.handle_ask_receipt(callback, bot)

    status_mock.assert_not_called()
    assert callback.answer.call_args.kwargs.get("show_alert") is True


def _live_ticket(**overrides):
    ticket = _fake_ticket(**overrides)
    ticket.live_chat = 1
    return ticket


async def test_staff_chatter_outside_live_chat_stays_internal():
    # A shift discussing a case in its own thread must not be broadcast to the
    # person the case is about.
    message = AsyncMock()
    message.message_thread_id = 42
    message.text = "он вчера уже писал, помнишь?"
    bot = AsyncMock()

    with patch.object(storage, "get_ticket_by_topic", AsyncMock(return_value=_fake_ticket())):
        await notify.relay_staff_reply(message, bot)

    bot.send_message.assert_not_called()


async def test_every_staff_message_is_relayed_while_live_chat_is_open():
    # The point of live chat: no button between each message.
    bot = AsyncMock()
    for text in ["Здравствуйте, разбираюсь.", "Проверил — вернём сегодня."]:
        message = AsyncMock()
        message.message_thread_id = 42
        message.text = text
        with patch.object(storage, "get_ticket_by_topic", AsyncMock(return_value=_live_ticket())):
            await notify.relay_staff_reply(message, bot)

    assert bot.send_message.call_count == 2
    assert "вернём сегодня" in bot.send_message.call_args.args[1]


async def test_opening_live_chat_tells_both_sides():
    callback = _fake_callback(f"{notify._REPLY_PREFIX}1")
    bot = AsyncMock()

    with patch.object(storage, "get_ticket", AsyncMock(return_value=_fake_ticket(forum_topic_id=42))), \
         patch.object(storage, "set_live_chat", AsyncMock()) as live_mock:
        await notify.handle_reply_request(callback, bot)

    live_mock.assert_called_once_with(1, True)
    recipients = [c.args[0] for c in bot.send_message.call_args_list]
    assert 123 in recipients  # the user learns a human joined
    assert notify.settings.support_staff_chat_id in recipients


async def test_closing_the_ticket_ends_the_live_chat():
    callback = _fake_callback(f"{notify._REJECT_PREFIX}1")
    bot = AsyncMock()
    api = AsyncMock()

    with patch.object(storage, "get_ticket", AsyncMock(return_value=_live_ticket())), \
         patch.object(storage, "resolve_escalation", AsyncMock()), \
         patch.object(storage, "set_ticket_status", AsyncMock()), \
         patch.object(storage, "set_live_chat", AsyncMock()) as live_mock:
        await notify.handle_escalation_decision(callback, bot, api)

    live_mock.assert_called_once_with(1, False)


def _plain_ticket(**overrides) -> storage.TicketRecord:
    defaults = dict(
        id=12, telegram_id="943402384", username="Niidaime", contact="+77001234567",
        problem_type="payment_error", apparat_name="Аппарат №1",
        raw_text="QR-код не появился на экране", transaction_id=None, status="open",
        draft_reply=None, forum_topic_id=None, payment_expected=1, live_chat=0, created_at="2026-06-20T12:00:00",
    )
    defaults.update(overrides)
    return storage.TicketRecord(**defaults)


def test_plain_escalation_includes_ticket_details():
    decision = Decision(action="escalate", reason="advice_not_helpful", staff_summary="Совет ИИ не помог юзеру.")
    text = notify.format_plain_escalation_text(12, _plain_ticket(), decision)
    assert "Аппарат №1" in text
    assert "943402384" in text
    assert "@Niidaime" in text
    assert "+77001234567" in text
    assert "QR-код не появился на экране" in text
    assert "Совет ИИ не помог юзеру." in text


def test_plain_escalation_handles_missing_optional_fields():
    decision = Decision(action="escalate", reason="advice_not_helpful", staff_summary="Совет ИИ не помог юзеру.")
    record = _plain_ticket(username=None, contact=None, raw_text=None)
    text = notify.format_plain_escalation_text(12, record, decision)
    assert "Юзернейм: -" in text
    assert "Контакт: -" in text


def test_plain_escalation_falls_back_when_ticket_not_found():
    decision = Decision(action="escalate", reason="diagnosis_failed", staff_summary="Не удалось собрать диагностику.")
    text = notify.format_plain_escalation_text(12, None, decision)
    assert text == "🆘 Заявка #12 - Не удалось собрать диагностику."


async def test_rejection_tells_the_user_the_case_is_closed():
    # Silence after "передал сотруднику, отвечу здесь" reads as being ignored.
    callback = _fake_callback(f"{notify._REJECT_PREFIX}1")
    bot = AsyncMock()
    api = AsyncMock()

    with patch.object(storage, "get_ticket", AsyncMock(return_value=_fake_ticket())), \
         patch.object(storage, "resolve_escalation", AsyncMock()), \
         patch.object(storage, "set_ticket_status", AsyncMock()):
        await notify.handle_escalation_decision(callback, bot, api)

    bot.send_message.assert_called_once()
    args, _ = bot.send_message.call_args
    assert args[0] == 123
    assert "#1" in args[1]


async def test_rejection_without_a_payment_points_at_the_receipt_route():
    # Nothing was found to refund - the useful next step is a fresh request
    # with a receipt, not "describe it again".
    callback = _fake_callback(f"{notify._REJECT_PREFIX}1")
    bot = AsyncMock()
    api = AsyncMock()

    with patch.object(storage, "get_ticket", AsyncMock(return_value=_fake_ticket(transaction_id=None))), \
         patch.object(storage, "resolve_escalation", AsyncMock()), \
         patch.object(storage, "set_ticket_status", AsyncMock()):
        await notify.handle_escalation_decision(callback, bot, api)

    args, _ = bot.send_message.call_args
    assert "чек" in args[1].lower()


async def test_rejection_still_completes_if_the_user_blocked_the_bot():
    callback = _fake_callback(f"{notify._REJECT_PREFIX}1")
    bot = AsyncMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("bot was blocked"))
    api = AsyncMock()

    with patch.object(storage, "get_ticket", AsyncMock(return_value=_fake_ticket())), \
         patch.object(storage, "resolve_escalation", AsyncMock()), \
         patch.object(storage, "set_ticket_status", AsyncMock()) as status_mock:
        await notify.handle_escalation_decision(callback, bot, api)

    status_mock.assert_called_once_with(1, "resolved_rejected")
    callback.message.edit_text.assert_called_once()


def test_closing_a_non_money_case_says_nothing_about_refunds():
    # Ticket #46 was "QR-код не появился" - no payment was ever in question, so
    # closing it with a paragraph about receipts shows we didn't read it.
    ticket = _fake_ticket(transaction_id=None)
    ticket.payment_expected = 0
    text = notify._closing_message(ticket)
    for word in ["возврат", "чек", "оплат"]:
        assert word not in text.lower(), text


def test_closing_a_paid_case_without_a_transaction_offers_the_receipt_route():
    ticket = _fake_ticket(transaction_id=None)
    text = notify._closing_message(ticket)
    assert "чек" in text.lower()


def test_closing_a_paid_case_with_a_transaction_explains_no_fault_was_found():
    text = notify._closing_message(_fake_ticket(transaction_id="tx-1"))
    assert "не нашли" in text.lower()


def test_close_button_drops_the_refund_wording_when_money_was_never_involved():
    money = notify._escalation_keyboard(1, can_refund=False, payment_expected=True)
    no_money = notify._escalation_keyboard(1, can_refund=False, payment_expected=False)
    money_label = money.inline_keyboard[0][0].text
    no_money_label = no_money.inline_keyboard[0][0].text
    assert "возврат" in money_label.lower()
    assert "возврат" not in no_money_label.lower()


def test_receipt_button_is_hidden_when_no_payment_could_exist():
    kb = notify._escalation_keyboard(1, can_refund=False, payment_expected=False)
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert not any(c.startswith(notify._ASK_RECEIPT_PREFIX) for c in callbacks)
    assert any(c.startswith(notify._REPLY_PREFIX) for c in callbacks)
