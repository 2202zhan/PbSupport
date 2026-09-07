import logging

from aiogram import Bot, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

import storage
from api_client import PrintBoxAPIClient, PrintBoxAPIError
from diagnosis import Evidence
from guard_rules import Decision

logger = logging.getLogger(__name__)

router = Router(name="notify")

_CONFIRM_PREFIX = "supportbot:confirm:"
_REJECT_PREFIX = "supportbot:reject:"


def _escalation_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить возврат", callback_data=f"{_CONFIRM_PREFIX}{ticket_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"{_REJECT_PREFIX}{ticket_id}"),
            ]
        ]
    )


def _format_escalation_text(ticket_id: int, evidence: Evidence, decision: Decision) -> str:
    t = evidence.ticket
    tx = evidence.transaction
    lines = [
        f"🆘 Заявка #{ticket_id}",
        f"Аппарат: {t.apparat_name_text}",
        f"Проблема: {t.problem_type}",
        f"Telegram ID: {t.telegram_id}",
        f"Юзернейм: @{t.username}" if t.username else "Юзернейм: -",
        f"Контакт: {t.contact or '-'}",
        "",
        f"Транзакция: {tx.id if tx else 'не найдена'}"
        + (f" ({tx.amount} ₸, {tx.date:%Y-%m-%d %H:%M})" if tx else ""),
        f"Идентификация подтверждена: {'да' if evidence.identity_confirmed else 'НЕТ'}",
        f"SNMP подтвердил печать: {evidence.print_signal_confirmed}",
        f"Ошибка скачивания в логах: {evidence.log_download_error}",
        f"Логи: печать завершена успешно: {evidence.log_print_success}",
        f"Подозрение на массовый сбой: {evidence.mass_outage_suspected} "
        f"({evidence.neighbor_failure_count}/{evidence.neighbor_total_checked} соседей без сигнала)",
        f"Состояние аппарата сейчас: "
        + (f"офлайн" if evidence.printer_currently_offline else "онлайн")
        + (f", тонер: {evidence.toner_levels}" if evidence.toner_levels else "")
        + (f", ошибка: {evidence.printer_error_text}" if evidence.printer_error_text else "")
        + (f", активный алерт: {evidence.apparat_active_alert}" if evidence.apparat_active_alert else ""),
        "",
        f"🤖 {decision.staff_summary or decision.reason}",
        "",
        f"Исходное сообщение юзера: {t.raw_text}",
    ]
    return "\n".join(lines)


async def send_escalation(bot: Bot, staff_chat_id: int, ticket_id: int, evidence: Evidence, decision: Decision) -> int:
    text = _format_escalation_text(ticket_id, evidence, decision)
    message = await bot.send_message(staff_chat_id, text, reply_markup=_escalation_keyboard(ticket_id))
    await storage.create_escalation(ticket_id, staff_chat_id, message.message_id)
    receipt_file_id = evidence.ticket.receipt_photo_file_id
    if receipt_file_id:
        send = bot.send_document if evidence.ticket.receipt_is_document else bot.send_photo
        await send(staff_chat_id, receipt_file_id, caption=f"📎 Чек к заявке #{ticket_id}")
    return message.message_id


@router.callback_query(lambda c: c.data and c.data.startswith((_CONFIRM_PREFIX, _REJECT_PREFIX)))
async def handle_escalation_decision(callback: CallbackQuery, bot: Bot, api: PrintBoxAPIClient) -> None:
    is_confirm = callback.data.startswith(_CONFIRM_PREFIX)
    ticket_id = int(callback.data.split(":")[-1])
    staff_name = callback.from_user.full_name

    ticket = await storage.get_ticket(ticket_id)
    if ticket is None:
        await callback.answer("Заявка не найдена", show_alert=True)
        return

    if ticket.status in ("resolved_refund", "resolved_rejected"):
        # The buttons stay visible on an already-resolved message (Telegram
        # doesn't remove them on its own) - a second tap, by the same person
        # or someone else in the group, must not refund/reject twice.
        await callback.answer("Эта заявка уже обработана", show_alert=True)
        return

    resolution = "confirmed_refund" if is_confirm else "rejected"
    await storage.resolve_escalation(callback.message.message_id, staff_name, resolution)

    if is_confirm:
        if not ticket.transaction_id:
            await callback.answer("Нет привязанной транзакции - возврат невозможен", show_alert=True)
            return
        try:
            await api.refund_transaction(ticket.transaction_id)
        except PrintBoxAPIError:
            logger.exception("Refund failed for ticket %s", ticket_id)
            await callback.answer("Ошибка при вызове возврата в API", show_alert=True)
            return
        await storage.record_decision(ticket_id, {}, None, None, None, "auto_refund")
        await storage.set_ticket_status(ticket_id, "resolved_refund")
        await bot.send_message(
            int(ticket.telegram_id),
            "Здравствуйте! Мы проверили вашу заявку — возврат средств подтверждён, "
            "деньги вернутся на счёт, с которого была оплата.",
        )
        new_text = callback.message.text + f"\n\n✅ Возврат подтверждён ({staff_name})"
    else:
        await storage.set_ticket_status(ticket_id, "resolved_rejected")
        new_text = callback.message.text + f"\n\n❌ Отклонено ({staff_name})"

    await callback.message.edit_text(new_text, reply_markup=None)
    await callback.answer()
