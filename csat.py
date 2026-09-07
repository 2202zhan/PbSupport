"""One-tap satisfaction poll sent after a ticket closes.

Telegram polls are non-anonymous here on purpose: without that the bot never
receives a poll_answer update and the poll would be decorative. The answer is
stored against the ticket so "how often did we actually help" becomes a number
instead of a guess from the escalation count.
"""

import logging

from aiogram import Bot, Router
from aiogram.types import PollAnswer

import storage

logger = logging.getLogger(__name__)

router = Router(name="csat")

# Order matters: the index of the chosen option is the score we store, so
# better outcomes come first.
_OPTIONS = ["👍 Да, всё решилось", "😐 Частично", "👎 Нет"]


async def send_poll(bot: Bot, chat_id: int, ticket_id: int) -> None:
    """Best-effort - neither a failed poll nor a failed write may break closing
    a ticket, so the whole thing is inside the guard."""
    try:
        message = await bot.send_poll(
            chat_id,
            question=f"Помогли ли мы по заявке #{ticket_id}?",
            options=_OPTIONS,
            is_anonymous=False,
        )
        if message.poll is not None:
            await storage.create_csat(ticket_id, message.poll.id)
    except Exception:
        logger.exception("could not send CSAT poll for ticket %s", ticket_id)


@router.poll_answer()
async def on_poll_answer(poll_answer: PollAnswer) -> None:
    if not poll_answer.option_ids:
        # Telegram sends an empty list when someone retracts their vote.
        return
    await storage.record_csat_score(poll_answer.poll_id, poll_answer.option_ids[0])
