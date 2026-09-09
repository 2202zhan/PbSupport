"""Where Telegram meets the agent.

Registered ahead of triage's router: whoever the gate admits is handled here
and never reaches the menu tree; everyone else falls through and the bot
behaves exactly as before.

The runtime decides; this module carries the decision out. Every path through
here ends with the user having been sent something.
"""

import json
import logging

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

import notify
import storage
from agent import files, memory, ui
from agent.gate import agent_enabled_for
from agent.runtime import run_turn
from agent.types import TurnContext, TurnResult
from api_client import PrintBoxAPIClient
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
    if await storage.find_live_chat_ticket(telegram_id) is not None:
        # A person is on the other end; /start must not silently drop them.
        await message.answer(
            "С вами сейчас переписывается сотрудник — пишите прямо здесь, он всё видит."
        )
        return
    conversation = await memory.current_conversation(telegram_id, message.from_user.username)
    await storage.close_conversation(conversation.id)
    await message.answer(_GREETING)


@router.message(F.text)
async def on_text(message: Message, bot: Bot, api: PrintBoxAPIClient) -> None:
    await _handle(bot, api, message, message.text)


@router.message(F.photo | F.document)
async def on_file(message: Message, bot: Bot, api: PrintBoxAPIClient) -> None:
    """A receipt is read the moment it arrives, asked for or not - people send
    proof when they have it, not when they are prompted."""
    telegram_id = str(message.from_user.id)
    conversation = await memory.current_conversation(telegram_id, message.from_user.username)
    fact = await files.read_and_remember(bot, message, conversation.id)
    await _handle(bot, api, message, fact)


@router.callback_query(F.data.startswith(ui.CALLBACK_PREFIX))
async def on_button(callback: CallbackQuery, bot: Bot, api: PrintBoxAPIClient) -> None:
    telegram_id = str(callback.from_user.id)
    conversation = await memory.current_conversation(telegram_id, callback.from_user.username)
    label = await ui.resolve(callback.data, conversation.id)
    await callback.answer()
    if label is None:
        # A button from an older conversation. Say so instead of quietly
        # answering something the user did not ask now.
        await callback.message.answer("Это из прошлого разговора. Напишите, что нужно сейчас.")
        return
    # Taking the keyboard away stops the same choice being sent twice and
    # leaves the thread readable.
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("could not clear the keyboard", exc_info=True)
    await _handle(bot, api, callback.message, label, from_user=callback.from_user)


async def _handle(
    bot: Bot, api: PrintBoxAPIClient, message: Message, text: str, from_user=None
) -> None:
    user = from_user or message.from_user
    telegram_id = str(user.id)

    # While a staff member has the conversation open, everything the user
    # writes goes to them. Answering over the top of a person mid-sentence is
    # worse than not answering at all.
    live = await storage.find_live_chat_ticket(telegram_id)
    if live is not None:
        try:
            await notify.relay_user_message(bot, live, message)
        except Exception:
            logger.exception("could not relay to staff for ticket %s", live.id)
            await message.answer("Не получилось передать сообщение, попробуйте ещё раз.")
        return
    # People send "не печатает" and "аппарат 3" a second apart; without this
    # the two turns read the same history and answer each other's question.
    async with memory.one_turn_at_a_time(telegram_id):
        conversation = await memory.current_conversation(telegram_id, user.username)
        await memory.remember_user_message(conversation.id, text)
        ctx = TurnContext(
            telegram_id=telegram_id,
            username=user.username,
            conversation_id=conversation.id,
            user_message=text,
            api=api,
            on_progress=_progress(message),
        )
        result = await run_turn(ctx)
        await deliver(bot, message, conversation.id, result, user=user, ctx=ctx)


def _progress(message: Message):
    """Lets a slow tool say it is working. The first note is a new message and
    the rest edit it, so a long check leaves one line in the chat rather than a
    running commentary - and a failure to post one never costs the answer."""
    sent: list[Message] = []

    async def _say(text: str) -> None:
        try:
            if sent:
                await sent[0].edit_text(text)
            else:
                sent.append(await message.answer(text))
        except Exception:
            logger.debug("could not post progress", exc_info=True)

    return _say


async def deliver(
    bot: Bot,
    message: Message,
    conversation_id: int,
    result: TurnResult,
    user=None,
    ctx: TurnContext | None = None,
) -> None:
    """Carries out what the turn decided. Whatever else happens, the user is
    answered - a turn that reached a human still has to say so."""
    user = user or message.from_user
    text = result.text or (_BROKEN if result.needs_staff else None)
    if text:
        # No keyboard once a human is on the case: the next message comes from
        # them, and quick answers to the bot would only get in the way.
        markup = None if result.needs_staff else await ui.keyboard(conversation_id, result.buttons)
        await message.answer(text, reply_markup=markup)
    await storage.set_conversation_expecting(
        conversation_id, "none" if result.needs_staff else result.expect
    )

    if not result.needs_staff:
        return

    try:
        await _hand_to_staff(bot, message, conversation_id, result, user, ctx)
    except Exception:
        # The user has already been told a human is coming, so this must not
        # look like a normal reply - it is a hole, and it has to be loud.
        logger.exception("could not escalate agent conversation %s", conversation_id)


async def _hand_to_staff(
    bot: Bot,
    message: Message,
    conversation_id: int,
    result: TurnResult,
    user,
    ctx: TurnContext | None,
) -> None:
    evidence = getattr(ctx, "evidence", None)
    review = getattr(ctx, "review", None)
    ticket_id = await _open_ticket(message, conversation_id, user, evidence)

    decision = Decision(
        action="recommend_refund" if result.refund_recommended else "escalate",
        reason=result.reason or "escalated_by_agent",
        # Everything the tools read this turn, which the model was never shown -
        # exact readings are useful to a person and only dangerous in a reply.
        staff_summary="\n".join(
            [result.staff_summary or "", *(getattr(ctx, "staff_notes", None) or [])]
        ).strip(),
        confidence=result.confidence,
        draft_reply=result.draft_reply,
    )

    if evidence is not None:
        # The full card, with the refund button if - and only if - the rules
        # allow one. The agent's recommendation is written on the card; whether
        # money can move is guard_rules' call and a person's tap.
        await notify.send_escalation(
            bot, settings.support_staff_chat_id, ticket_id, evidence, decision, review
        )
        return

    await notify.send_plain_escalation(bot, settings.support_staff_chat_id, ticket_id, decision)
    await _attach_receipt(bot, ticket_id, conversation_id)


async def _attach_receipt(bot: Bot, ticket_id: int, conversation_id: int) -> None:
    """The file the user sent belongs on the card - a summary of a receipt is
    not a receipt, and staff have to be able to look at it."""
    conversation = await storage.get_conversation(conversation_id)
    if conversation is None or not conversation.receipt:
        return
    stored = json.loads(conversation.receipt)
    try:
        await notify.forward_receipt(
            bot, ticket_id, stored["file_id"], bool(stored.get("is_document"))
        )
    except Exception:
        logger.warning("could not attach the receipt to ticket %s", ticket_id, exc_info=True)


async def _open_ticket(message: Message, conversation_id: int, user, evidence=None) -> int:
    """A ticket is created here, at the moment a human is actually needed -
    not at the start of every conversation."""
    transcript = await memory.history(conversation_id)
    said = [m["content"] for m in transcript if m.get("role") == "user" and m.get("content")]
    transaction = getattr(evidence, "transaction", None)
    apparat = getattr(evidence, "apparat", None)
    ticket_id = await storage.create_ticket(
        telegram_id=str(user.id),
        username=user.username,
        contact=None,
        problem_type="agent",
        apparat_name=(
            transaction.machine if transaction else (apparat.name_apparat if apparat else None)
        ),
        raw_text="\n".join(said[-5:]) or (message.text or ""),
        # Money was on the table only where a payment was actually found; the
        # closing message and the "ask for a receipt" button both read wrong
        # otherwise.
        payment_expected=transaction is not None,
    )
    if transaction is not None:
        await storage.set_ticket_transaction(ticket_id, transaction.id)
    return ticket_id
