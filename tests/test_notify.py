from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


def _fake_ticket(status: str = "escalated", transaction_id: str | None = "tx-1") -> storage.TicketRecord:
    return storage.TicketRecord(
        id=1, telegram_id="123", username="user", contact=None,
        problem_type="not_printed", apparat_name="Аппарат №1", raw_text="не печатает",
        transaction_id=transaction_id, status=status, created_at="2026-06-29T12:00:00",
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
         patch.object(storage, "set_ticket_status", AsyncMock()) as set_status_mock:
        await notify.handle_escalation_decision(callback, bot, api)

    api.refund_transaction.assert_called_once_with("tx-1")
    set_status_mock.assert_called_once_with(1, "resolved_refund")
    callback.message.edit_text.assert_called_once()
    _, kwargs = callback.message.edit_text.call_args
    assert kwargs.get("reply_markup") is None
