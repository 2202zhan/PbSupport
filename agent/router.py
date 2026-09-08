"""Where Telegram meets the agent.

Registered ahead of triage's router: whoever the gate admits is handled here
and never reaches the menu tree; everyone else falls through and the bot
behaves exactly as before.

The runtime decides; this module carries the decision out. Every path through
here ends with the user having been sent something.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

import notify
import storage
from agent import memory
from agent.gate import agent_enabled_for
from agent.runtime import run_turn
from agent.types import TurnContext, TurnResult
from config import settings
from guard_rules import Decision

logger = logging.getLogger(__name__)

router = Router(name="agent")

_GREETING = (
    "Здравствуйте! Я поддержка PrintBox.\n\n"
    "Опишите, пожалуйста, своими словами, что случилось — я разберусь. "
    "Можно просто текстом, как есть."
)

_BROKEN = (
    "Что-то у меня сломалось на этом сообщении. Напишите ещё раз, пожалуйста, "
    "или чуть подробнее — я разберусь."
)


def from_admitted_user(event: Message | CallbackQuery) -> bool:
    return event.from_user is not None and agent_enabled_for(event.from_user.id)


# Private chats only, for the same reason triage does it: with privacy mode off
# the bot sees every message in the staff group, and none of those are support
# requests.
router.message.filter(F.chat.type == "private", from_admitted_user)
router.callback_query.filter(from_admitted_user)


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    """A fresh start means a fresh conversation - otherwise /start would carry
    yesterday's context into a new problem."""
    telegram_id = str(message.from_user.id)
    conversation = await memory.current_conversation(telegram_id, message.from_user.username)
    await storage.close_conversation(conversation.id)
    await message.answer(_GREETING)


@router.message(F.text)
async def on_text(message: Message, bot: Bot) -> None:
    telegram_id = str(message.from_user.id)
    # People send "не печатает" and "аппарат 3" a second apart; without this
    # the two turns read the same history and answer each other's question.
    async with memory.one_turn_at_a_time(telegram_id):
        conversation = await memory.current_conversation(telegram_id, message.from_user.username)
        await memory.remember_user_message(conversation.id, message.text)
        result = await run_turn(
            TurnContext(
                telegram_id=telegram_id,
                username=message.from_user.username,
                conversation_id=conversation.id,
                user_message=message.text,
            )
        )
        await deliver(bot, message, conversation.id, result)


async def deliver(bot: Bot, message: Message, conversation_id: int, result: TurnResult) -> None:
    """Carries out what the turn decided. Whatever else happens, the user is
    answered - a turn that reached a human still has to say so."""
    if result.text:
        await message.answer(result.text)
    elif result.needs_staff:
        await message.answer(_BROKEN)

    if not result.needs_staff:
        return

    try:
        ticket_id = await _open_ticket(message, conversation_id)
        await notify.send_plain_escalation(
            bot,
            settings.support_staff_chat_id,
            ticket_id,
            Decision(
                action="escalate",
                reason=result.reason or "escalated_by_agent",
                staff_summary=result.staff_summary,
            ),
        )
    except Exception:
        # The user has already been told a human is coming, so this must not
        # look like a normal reply - it is a hole, and it has to be loud.
        logger.exception("could not escalate agent conversation %s", conversation_id)


async def _open_ticket(message: Message, conversation_id: int) -> int:
    """A ticket is created here, at the moment a human is actually needed -
    not at the start of every conversation."""
    transcript = await memory.history(conversation_id)
    said = [m["content"] for m in transcript if m.get("role") == "user" and m.get("content")]
    return await storage.create_ticket(
        telegram_id=str(message.from_user.id),
        username=message.from_user.username,
        contact=None,
        problem_type="agent",
        apparat_name=None,
        raw_text="\n".join(said[-5:]) or message.text,
        payment_expected=False,
    )
