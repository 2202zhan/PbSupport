"""Reading a file the user sent, into something the conversation can carry.

A receipt is the one piece of ground truth in a support case: it can contradict
the ticket outright, and it is what finds a payment made from another account.
So it is read as soon as it arrives, whether or not the agent asked for one -
people send proof when they have it, not when they are prompted.
"""

import logging

import receipts
import storage

logger = logging.getLogger(__name__)


async def read_and_remember(bot, message, conversation_id: int) -> str:
    """Stores what the file is and returns the sentence that goes into the
    conversation as the user's message."""
    file_id, is_document, parsed = await receipts.extract(bot, message)
    if not file_id:
        return "[юзер прислал файл, который не получилось открыть]"

    stored = {"file_id": file_id, "is_document": is_document}
    if parsed is not None:
        stored["amount"] = parsed.amount
        stored["paid_at"] = parsed.paid_at.isoformat() if parsed.paid_at else None
    await storage.set_conversation_receipt(conversation_id, stored)

    caption = f", подпись: {message.caption}" if message.caption else ""
    if parsed is None:
        # Photos have no OCR here. Saying so is better than letting the model
        # assume the numbers were read and answer as if they were.
        kind = "фото" if not is_document else "файл"
        return (
            f"[юзер прислал {kind} чека{caption}; сумму и дату с него прочитать не удалось, "
            "сотрудник посмотрит его глазами]"
        )

    when = f"{parsed.paid_at:%d.%m в %H:%M}" if parsed.paid_at else "дата не читается"
    amount = f"{parsed.amount:g} ₸" if parsed.amount is not None else "сумма не читается"
    if receipts.is_stale(parsed.paid_at):
        return (
            f"[юзер прислал чек: {amount}, {when} — это больше суток назад. Такие обращения "
            "мы проверить уже не можем: технических данных за тот период не осталось. Скажи "
            "это прямо и предложи прислать чек именно за сегодняшний случай, если он был]"
        )
    return f"[юзер прислал чек: {amount}, {when}]"
