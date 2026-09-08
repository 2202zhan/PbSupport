"""What the agent remembers between messages.

storage.py owns the SQL; this module owns the policy - when a conversation is
still the same conversation, how two messages from the same person are kept
from racing each other, and how a long history is folded down so the prompt
does not grow without bound.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

import storage
from config import settings

logger = logging.getLogger(__name__)

# Messages always kept verbatim at the end of the history. Folding the most
# recent exchange into prose would lose exactly the details the next turn needs.
_KEEP_VERBATIM = 8


async def current_conversation(telegram_id: str, username: str | None = None) -> storage.Conversation:
    """The conversation this message belongs to, starting a new one if the last
    went quiet longer ago than the TTL."""
    existing = await storage.active_conversation(
        telegram_id, settings.agent_conversation_ttl_minutes
    )
    if existing is not None:
        return existing
    conversation_id = await storage.start_conversation(telegram_id, username)
    conversation = await storage.get_conversation(conversation_id)
    assert conversation is not None  # just inserted
    return conversation


async def remember_user_message(conversation_id: int, text: str) -> int:
    return await storage.append_message(conversation_id, role="user", content=text)


async def remember_assistant_message(
    conversation_id: int, text: str | None, tool_calls: list[dict] | None = None
) -> int:
    return await storage.append_message(
        conversation_id,
        role="assistant",
        content=text,
        tool_calls=json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
    )


async def remember_tool_result(
    conversation_id: int, tool_name: str, tool_call_id: str, result: object
) -> int:
    return await storage.append_message(
        conversation_id,
        role="tool",
        content=result if isinstance(result, str) else json.dumps(result, ensure_ascii=False),
        tool_name=tool_name,
        tool_call_id=tool_call_id,
    )


async def history(conversation_id: int) -> list[dict]:
    """The conversation in the shape the chat API expects: the summary of what
    was folded away, then every message since, replayed as it happened."""
    conversation = await storage.get_conversation(conversation_id)
    if conversation is None:
        return []

    messages: list[dict] = []
    if conversation.summary:
        messages.append(
            {"role": "system", "content": f"Ранее в этом разговоре: {conversation.summary}"}
        )
    for m in await storage.conversation_messages(conversation_id, after_id=conversation.summarised_upto):
        messages.append(_as_api_message(m))
    return messages


def _as_api_message(m: storage.ConversationMessage) -> dict:
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id or "", "content": m.content or ""}
    if m.role == "assistant" and m.tool_calls:
        return {"role": "assistant", "content": m.content, "tool_calls": json.loads(m.tool_calls)}
    return {"role": m.role, "content": m.content or ""}


async def compact_if_needed(
    conversation_id: int, summarise: Callable[[list[dict]], Awaitable[str]]
) -> bool:
    """Folds the head of a long history into prose. Returns whether it did.

    The summariser is passed in rather than called directly: this module has no
    business knowing which model writes the summary, and tests need it to be
    nothing at all.
    """
    conversation = await storage.get_conversation(conversation_id)
    if conversation is None:
        return False
    pending = await storage.conversation_messages(conversation_id, after_id=conversation.summarised_upto)
    if len(pending) <= settings.agent_history_max_messages:
        return False

    cut = len(pending) - _KEEP_VERBATIM
    # A tool result whose call has been folded away is an orphan the API will
    # reject, so never cut between an assistant's tool call and its results.
    while cut > 0 and pending[cut].role == "tool":
        cut -= 1
    if cut <= 0:
        return False
    fold = pending[:cut]

    try:
        summary = await summarise([_as_api_message(m) for m in fold])
    except Exception:
        logger.exception("could not summarise conversation %s - keeping it verbatim", conversation_id)
        return False
    if not summary:
        return False

    if conversation.summary:
        summary = f"{conversation.summary}\n{summary}"
    await storage.set_conversation_summary(conversation_id, summary, fold[-1].id)
    return True


_locks: dict[str, asyncio.Lock] = {}
_waiting: dict[str, int] = {}


@asynccontextmanager
async def one_turn_at_a_time(telegram_id: str):
    """People send "не печатает" and "аппарат 3" as two messages a second apart.
    Without this the two turns run at once, read the same history, and answer
    each other's question."""
    lock = _locks.setdefault(telegram_id, asyncio.Lock())
    _waiting[telegram_id] = _waiting.get(telegram_id, 0) + 1
    try:
        async with lock:
            yield
    finally:
        _waiting[telegram_id] -= 1
        if _waiting[telegram_id] <= 0:
            _waiting.pop(telegram_id, None)
            _locks.pop(telegram_id, None)
