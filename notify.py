"""The staff side: a decision card in the support chat, and the buttons on it.

This is the only module in the codebase that calls the refund API, and it does
so exclusively from a callback a person tapped. The AI reaches this file with a
recommendation and a draft reply; nothing here fires on its own.
"""

import logging

from aiogram import Bot, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

import csat
import storage
from api_client import PrintBoxAPIClient, PrintBoxAPIError
from diagnosis import Evidence
from guard_rules import Decision, RefundReview

logger = logging.getLogger(__name__)

router = Router(name="notify")

_CONFIRM_PREFIX = "supportbot:confirm:"
_REJECT_PREFIX = "supportbot:reject:"

_CONFIDENCE_LABELS = {"high": "высокая", "medium": "средняя", "low": "низкая"}

_FALLBACK_REFUND_REPLY = (
    "Здравствуйте! Мы проверили вашу заявку — возврат средств подтверждён, "
    "деньги вернутся на счёт, с которого была оплата."
)


def _escalation_keyboard(ticket_id: int, can_refund: bool) -> InlineKeyboardMarkup:
    if not can_refund:
        # Refunding would be wrong or would simply fail here (see
        # guard_rules.review_refund_case) - don't offer a button that lies.
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Закрыть без возврата", callback_data=f"{_REJECT_PREFIX}{ticket_id}")]
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить возврат", callback_data=f"{_CONFIRM_PREFIX}{ticket_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"{_REJECT_PREFIX}{ticket_id}"),
            ]
        ]
    )


def _format_escalation_text(
    ticket_id: int, evidence: Evidence, decision: Decision, review: RefundReview | None = None
) -> str:
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
    ]

    if decision.action == "recommend_refund":
        confidence = _CONFIDENCE_LABELS.get(decision.confidence or "low", decision.confidence or "?")
        lines.append(f"🤖 Рекомендую возврат · уверенность: {confidence}")
        lines.append(decision.reason)
    else:
        lines.append(f"🤖 {decision.staff_summary or decision.reason}")

    if review and review.blockers:
        lines.append("")
        lines.append("🚫 Возврат подтвердить нельзя:")
        lines.extend(f"• {label}" for label in review.blocker_labels)
    if review and review.warnings:
        lines.append("")
        lines.append("⚠️ Обратите внимание:")
        lines.extend(f"• {label}" for label in review.warning_labels)

    if decision.draft_reply:
        lines.append("")
        lines.append("✉️ Отправим юзеру после подтверждения:")
        lines.append(f"«{decision.draft_reply}»")

    lines.append("")
    lines.append(f"Исходное сообщение юзера: {t.raw_text}")
    return "\n".join(lines)


async def send_escalation(
    bot: Bot,
    staff_chat_id: int,
    ticket_id: int,
    evidence: Evidence,
    decision: Decision,
    review: RefundReview | None = None,
) -> int:
    if decision.draft_reply:
        await storage.set_ticket_draft_reply(ticket_id, decision.draft_reply)
    text = _format_escalation_text(ticket_id, evidence, decision, review)
    can_refund = bool(review.can_refund) if review is not None else bool(evidence.transaction)
    message = await bot.send_message(
        staff_chat_id, text, reply_markup=_escalation_keyboard(ticket_id, can_refund)
    )
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
        # The transaction_id goes into the evidence blob so was_already_refunded()
        # can find this refund later - it's the only place refunds are recorded.
        await storage.record_decision(
            ticket_id, {"transaction_id": ticket.transaction_id}, None, None, None, "refund_confirmed"
        )
        await storage.set_ticket_status(ticket_id, "resolved_refund")
        await bot.send_message(int(ticket.telegram_id), ticket.draft_reply or _FALLBACK_REFUND_REPLY)
        await csat.send_poll(bot, int(ticket.telegram_id), ticket_id)
        new_text = callback.message.text + f"\n\n✅ Возврат подтверждён ({staff_name})"
    else:
        await storage.set_ticket_status(ticket_id, "resolved_rejected")
        new_text = callback.message.text + f"\n\n❌ Отклонено ({staff_name})"

    await callback.message.edit_text(new_text, reply_markup=None)
    await callback.answer()
