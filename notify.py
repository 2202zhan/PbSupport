"""The staff side: one forum topic per ticket, with a decision card inside it.

This is the only module in the codebase that calls the refund API, and it does
so exclusively from a callback a person tapped. The AI reaches this file with a
recommendation and a draft reply; nothing here fires on its own.

Each ticket gets its own thread in the staff supergroup, so discussion about
one case doesn't bury another, and the thread closes when the case does. If the
group isn't a forum (or the bot lacks "manage topics"), everything still works -
messages just land in the main chat as before.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import csat
import storage
from api_client import PrintBoxAPIClient, PrintBoxAPIError
from config import settings
from diagnosis import Evidence
from guard_rules import Decision, RefundReview

logger = logging.getLogger(__name__)

router = Router(name="notify")

_CONFIRM_PREFIX = "supportbot:confirm:"
_REJECT_PREFIX = "supportbot:reject:"
_ASK_RECEIPT_PREFIX = "supportbot:askreceipt:"
_REPLY_PREFIX = "supportbot:reply:"

_CONFIDENCE_LABELS = {"high": "высокая", "medium": "средняя", "low": "низкая"}

_SHORT_PROBLEM = {
    "not_printed": "не печатает",
    "payment_error": "ошибка оплаты",
    "print_quality": "качество печати",
    "upload_failed": "файл не загрузился",
    "device_issue": "бумага/тонер",
    "other": "другое",
}

_FALLBACK_REFUND_REPLY = (
    "Здравствуйте! Мы проверили вашу заявку — возврат средств подтверждён, "
    "деньги вернутся на счёт, с которого была оплата."
)

_RECEIPT_REQUEST_TEMPLATE = (
    "Здравствуйте! По вашей заявке #{ticket_id} сотруднику нужен чек оплаты, чтобы "
    "закончить проверку. Пришлите, пожалуйста, фото или PDF чека прямо сюда."
)

# A rejection has to reach the user too - the bot promised "отвечу здесь", and
# silence after that reads as being ignored. Two wordings, because "we couldn't
# find your payment at all" and "we found it and saw no fault" are different
# news and deserve different next steps.
_REJECTED_NO_PAYMENT_TEMPLATE = (
    "🔍 Заявка #{ticket_id} закрыта: подтвердить оплату по этому заказу не удалось, "
    "поэтому оформить возврат мы не можем.\n\n"
    "Если у вас сохранился чек — отправьте /start и создайте обращение заново, приложив "
    "его. С чеком мы сможем найти платёж, даже если он был с другого аккаунта."
)

_REJECTED_TEMPLATE = (
    "🔍 Заявка #{ticket_id} закрыта: мы проверили — технической ошибки с нашей стороны "
    "не нашли, поэтому возврат по ней не оформляем.\n\n"
    "Если считаете, что это ошибка, отправьте /start и опишите ситуацию подробнее — "
    "посмотрим ещё раз."
)

# Threads where a staff member tapped "Ответить" and their next message should
# go to the user. Deliberately in memory: it lives for seconds, and losing it
# on restart just means the message isn't relayed - nothing breaks.
_awaiting_staff_reply: set[int] = set()


def _escalation_keyboard(ticket_id: int, can_refund: bool) -> InlineKeyboardMarkup:
    rows = []
    if can_refund:
        rows.append(
            [
                InlineKeyboardButton(text="✅ Вернуть", callback_data=f"{_CONFIRM_PREFIX}{ticket_id}"),
                InlineKeyboardButton(text="❌ Отказать", callback_data=f"{_REJECT_PREFIX}{ticket_id}"),
            ]
        )
    else:
        # Refunding would be wrong or would simply fail here (see
        # guard_rules.review_refund_case) - don't offer a button that lies.
        rows.append(
            [InlineKeyboardButton(text="❌ Закрыть без возврата", callback_data=f"{_REJECT_PREFIX}{ticket_id}")]
        )
    rows.append(
        [
            InlineKeyboardButton(text="📎 Запросить чек", callback_data=f"{_ASK_RECEIPT_PREFIX}{ticket_id}"),
            InlineKeyboardButton(text="✍️ Ответить", callback_data=f"{_REPLY_PREFIX}{ticket_id}"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _topic_title(ticket_id: int, problem_type: str, apparat_name: str | None) -> str:
    problem = _SHORT_PROBLEM.get(problem_type, problem_type)
    parts = [f"#{ticket_id}", problem]
    if apparat_name:
        parts.append(apparat_name)
    return " · ".join(parts)[:128]


async def _open_topic(bot: Bot, chat_id: int, ticket_id: int, title: str) -> int | None:
    """Returns the thread id, or None when topics aren't available - callers
    then post into the main chat, which is what happened before topics."""
    try:
        topic = await bot.create_forum_topic(chat_id, name=title)
    except Exception:
        logger.warning("could not open a forum topic for ticket %s", ticket_id, exc_info=True)
        return None
    await storage.set_ticket_topic(ticket_id, topic.message_thread_id)
    return topic.message_thread_id


async def _close_topic(bot: Bot, chat_id: int, thread_id: int | None) -> None:
    if thread_id is None:
        return
    try:
        await bot.close_forum_topic(chat_id, thread_id)
    except Exception:
        logger.warning("could not close forum topic %s", thread_id, exc_info=True)


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


def format_plain_escalation_text(ticket_id: int, ticket_record, decision: Decision) -> str:
    """For tickets that never built an Evidence object (scripted replies, a
    diagnosis that failed early) - staff still get apparat, contact and the
    original complaint rather than one bare line."""
    if ticket_record is None:
        return f"🆘 Заявка #{ticket_id} - {decision.staff_summary or decision.reason}"
    problem = _SHORT_PROBLEM.get(ticket_record.problem_type, ticket_record.problem_type)
    lines = [
        f"🆘 Заявка #{ticket_id}",
        f"Аппарат: {ticket_record.apparat_name or '-'}",
        f"Категория: {problem}",
        f"Telegram ID: {ticket_record.telegram_id}",
        f"Юзернейм: @{ticket_record.username}" if ticket_record.username else "Юзернейм: -",
        f"Контакт: {ticket_record.contact or '-'}",
        "",
        f"🤖 {decision.staff_summary or decision.reason}",
        "",
        f"Исходное сообщение юзера: {ticket_record.raw_text or '-'}",
    ]
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
    thread_id = await _open_topic(
        bot,
        staff_chat_id,
        ticket_id,
        _topic_title(ticket_id, evidence.ticket.problem_type, evidence.ticket.apparat_name_text),
    )
    text = _format_escalation_text(ticket_id, evidence, decision, review)
    can_refund = bool(review.can_refund) if review is not None else bool(evidence.transaction)
    message = await bot.send_message(
        staff_chat_id,
        text,
        reply_markup=_escalation_keyboard(ticket_id, can_refund),
        message_thread_id=thread_id,
    )
    await storage.create_escalation(ticket_id, staff_chat_id, message.message_id)
    receipt_file_id = evidence.ticket.receipt_photo_file_id
    if receipt_file_id:
        send = bot.send_document if evidence.ticket.receipt_is_document else bot.send_photo
        await send(
            staff_chat_id,
            receipt_file_id,
            caption=f"📎 Чек к заявке #{ticket_id}",
            message_thread_id=thread_id,
        )
    return message.message_id


async def send_plain_escalation(bot: Bot, staff_chat_id: int, ticket_id: int, decision: Decision) -> None:
    """Escalation for a ticket with no Evidence - same thread treatment, but a
    text-only card and no refund buttons, since there's nothing to refund against."""
    ticket_record = await storage.get_ticket(ticket_id)
    thread_id = ticket_record.forum_topic_id if ticket_record else None
    if thread_id is None and ticket_record is not None:
        thread_id = await _open_topic(
            bot,
            staff_chat_id,
            ticket_id,
            _topic_title(ticket_id, ticket_record.problem_type, ticket_record.apparat_name),
        )
    message = await bot.send_message(
        staff_chat_id,
        format_plain_escalation_text(ticket_id, ticket_record, decision),
        reply_markup=_escalation_keyboard(ticket_id, can_refund=False),
        message_thread_id=thread_id,
    )
    await storage.create_escalation(ticket_id, staff_chat_id, message.message_id)


async def forward_receipt(bot: Bot, ticket_id: int, file_id: str, is_document: bool) -> None:
    """Puts a receipt into the ticket's own thread, so it sits with the case
    rather than at the bottom of the group."""
    ticket = await storage.get_ticket(ticket_id)
    send = bot.send_document if is_document else bot.send_photo
    await send(
        settings.support_staff_chat_id,
        file_id,
        caption=f"📎 Чек к заявке #{ticket_id}",
        message_thread_id=ticket.forum_topic_id if ticket else None,
    )


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
        template = _REJECTED_TEMPLATE if ticket.transaction_id else _REJECTED_NO_PAYMENT_TEMPLATE
        try:
            await bot.send_message(
                int(ticket.telegram_id), template.format(ticket_id=ticket_id)
            )
        except Exception:
            logger.exception("could not tell the user ticket %s was rejected", ticket_id)
        new_text = callback.message.text + f"\n\n❌ Отклонено ({staff_name})"

    # The refund decision is final, but the conversation isn't: staff keep the
    # reply button so they can explain a rejection in their own words.
    await callback.message.edit_text(
        new_text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✍️ Ответить", callback_data=f"{_REPLY_PREFIX}{ticket_id}")]
            ]
        ),
    )
    _awaiting_staff_reply.discard(ticket.forum_topic_id)
    await _close_topic(bot, settings.support_staff_chat_id, ticket.forum_topic_id)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith(_ASK_RECEIPT_PREFIX))
async def handle_ask_receipt(callback: CallbackQuery, bot: Bot) -> None:
    ticket_id = int(callback.data.split(":")[-1])
    ticket = await storage.get_ticket(ticket_id)
    if ticket is None:
        await callback.answer("Заявка не найдена", show_alert=True)
        return

    try:
        await bot.send_message(
            int(ticket.telegram_id), _RECEIPT_REQUEST_TEMPLATE.format(ticket_id=ticket_id)
        )
    except Exception:
        logger.exception("could not ask user for a receipt on ticket %s", ticket_id)
        await callback.answer("Не удалось написать юзеру", show_alert=True)
        return

    await storage.set_ticket_status(ticket_id, "awaiting_receipt")
    await bot.send_message(
        settings.support_staff_chat_id,
        f"📎 Попросил юзера прислать чек ({callback.from_user.full_name}). "
        "Как пришлёт — положу сюда же.",
        message_thread_id=ticket.forum_topic_id,
    )
    await callback.answer("Запросил чек у юзера")


@router.callback_query(lambda c: c.data and c.data.startswith(_REPLY_PREFIX))
async def handle_reply_request(callback: CallbackQuery, bot: Bot) -> None:
    ticket_id = int(callback.data.split(":")[-1])
    ticket = await storage.get_ticket(ticket_id)
    if ticket is None:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    if ticket.forum_topic_id is None:
        await callback.answer(
            "Ответ через бота работает только в теме заявки — включите темы в группе",
            show_alert=True,
        )
        return

    # The thread is closed once the case is resolved, and a closed topic only
    # accepts messages from admins - reopen it so anyone on shift can type.
    try:
        await bot.reopen_forum_topic(settings.support_staff_chat_id, ticket.forum_topic_id)
    except Exception:
        logger.debug("topic %s was already open", ticket.forum_topic_id, exc_info=True)

    _awaiting_staff_reply.add(ticket.forum_topic_id)
    await bot.send_message(
        settings.support_staff_chat_id,
        "✍️ Напишите следующим сообщением в этой теме — я передам его юзеру дословно.",
        message_thread_id=ticket.forum_topic_id,
    )
    await callback.answer()


@router.message(F.chat.id == settings.support_staff_chat_id, F.message_thread_id, F.text)
async def relay_staff_reply(message: Message, bot: Bot) -> None:
    """Only relays after someone tapped "Ответить" in this thread - otherwise
    staff couldn't discuss a case among themselves without the user seeing it."""
    thread_id = message.message_thread_id
    if thread_id not in _awaiting_staff_reply:
        return
    ticket = await storage.get_ticket_by_topic(thread_id)
    if ticket is None:
        return

    _awaiting_staff_reply.discard(thread_id)
    try:
        await bot.send_message(
            int(ticket.telegram_id), f"💬 Сотрудник поддержки:\n\n{message.text}"
        )
    except Exception:
        logger.exception("could not relay staff reply for ticket %s", ticket.id)
        await message.reply("Не удалось доставить — юзер мог заблокировать бота.")
        return
    await message.reply("Отправил юзеру ✅")
