"""Entry point for the agent path.

Registered ahead of triage's router: when the gate admits a user, their update
is handled here and never reaches the menu tree; when it doesn't, aiogram falls
through to triage and the bot behaves exactly as before.

The handlers themselves arrive in phase 2. Until then this router has nothing
to handle, so phase 0 is a no-op for everyone - the point of it is that the
switch, the gate and the bookkeeping are in place and under test before any
behaviour hangs off them.
"""

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from agent.gate import agent_enabled_for

router = Router(name="agent")


def from_admitted_user(event: Message | CallbackQuery) -> bool:
    return event.from_user is not None and agent_enabled_for(event.from_user.id)


# Private chats only, for the same reason triage does it: with privacy mode off
# the bot sees every message in the staff group, and none of those are support
# requests.
router.message.filter(F.chat.type == "private", from_admitted_user)
router.callback_query.filter(from_admitted_user)
