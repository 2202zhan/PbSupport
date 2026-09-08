"""Drawing the agent's answer in Telegram.

The keyboard is per-message, built from what the agent asked for on this turn -
there is no fixed menu anywhere. Labels are stored and the button carries only
its id, because Telegram allows 64 bytes of callback data and a Russian label
does not fit.
"""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import storage

CALLBACK_PREFIX = "ab:"

# The way out of any conversation, on every message the agent sends. Typing
# "позовите человека" works too, but someone who has stopped trusting the bot
# should not have to phrase a request to escape it.
CALL_HUMAN = "✋ Позвать человека"


async def keyboard(conversation_id: int, labels: list[str]) -> InlineKeyboardMarkup:
    saved = await storage.save_buttons(conversation_id, [*labels, CALL_HUMAN])
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"{CALLBACK_PREFIX}{button_id}")]
        for label, button_id in zip([*labels, CALL_HUMAN], saved)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def resolve(callback_data: str, conversation_id: int) -> str | None:
    """What the tapped button said, or None if it belongs to another
    conversation - a button from a closed case must not reopen it silently."""
    raw = callback_data.removeprefix(CALLBACK_PREFIX)
    if not raw.isdigit():
        return None
    button = await storage.get_button(int(raw))
    if button is None or button["conversation_id"] != conversation_id:
        return None
    return button["label"]
