from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import csat
import notify
import storage


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
) -> storage.TicketRecord:
    return storage.TicketRecord(
        id=1, telegram_id="123", username="user", contact=None,
        problem_type="not_printed", apparat_name="Аппарат №1", raw_text="не печатает",
        transaction_id=transaction_id, status=status, draft_reply=draft_reply,
        created_at="2026-06-29T12:00:00",
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
    assert kwargs.get("reply_markup") is None


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
    labels = [b.text for row in keyboard.inline_keyboard for b in row]
    assert not any("возврат" in label.lower() and "подтвердить" in label.lower() for label in labels)
    assert len(labels) == 1


def test_clean_case_offers_both_buttons():
    keyboard = notify._escalation_keyboard(1, can_refund=True)
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert callbacks == [f"{notify._CONFIRM_PREFIX}1", f"{notify._REJECT_PREFIX}1"]
