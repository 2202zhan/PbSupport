import asyncio
import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from config import settings

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id TEXT NOT NULL,
    username TEXT,
    contact TEXT,
    problem_type TEXT NOT NULL,
    apparat_name TEXT,
    raw_text TEXT,
    transaction_id TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    draft_reply TEXT,
    forum_topic_id INTEGER,
    payment_expected INTEGER NOT NULL DEFAULT 1,
    live_chat INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    evidence_json TEXT NOT NULL,
    ai_action TEXT,
    ai_reasoning TEXT,
    guard_triggered TEXT,
    final_action TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    staff_chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    resolved_by TEXT,
    resolution TEXT,
    resolved_at TEXT,
    created_at TEXT NOT NULL
);

-- One conversation is one continuous exchange with a user. It stays open until
-- it goes quiet for the TTL (see agent/memory.py), so a user who comes back an
-- hour later is not made to explain themselves from the start.
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id TEXT NOT NULL,
    username TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    -- The head of a long history, folded into prose so the model keeps the
    -- thread without carrying every message forward forever.
    summary TEXT,
    summarised_upto INTEGER NOT NULL DEFAULT 0,
    -- What the agent asked for last: "text", "choice", "file" or "none".
    expecting TEXT NOT NULL DEFAULT 'text',
    -- The receipt this person sent, as JSON: the Telegram file id so it can be
    -- put on the staff card later, and whatever was read out of it. Kept on the
    -- conversation rather than on a turn, because the escalation that needs it
    -- may be several messages away.
    receipt TEXT,
    created_at TEXT NOT NULL,
    last_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(telegram_id, status, last_at);

CREATE TABLE IF NOT EXISTS conversation_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    role TEXT NOT NULL,
    content TEXT,
    -- Set on tool results (which tool answered) and on assistant turns that
    -- called tools (the calls themselves, as JSON), so the exchange can be
    -- replayed to the model exactly as it happened.
    tool_name TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversation_messages_conv
    ON conversation_messages(conversation_id, id);

-- A tapped button has to be resolved back into what it said, and Telegram
-- only carries 64 bytes of callback data - not enough for a Russian label. So
-- the label lives here and the button carries its id.
CREATE TABLE IF NOT EXISTS conversation_buttons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    label TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- One row per turn the agent takes, written whether the turn succeeded or
-- failed. This is how the new path gets compared with the old one instead of
-- guessed about: what came in, which tools ran, what went out, how long it
-- took and what it cost.
CREATE TABLE IF NOT EXISTS agent_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER,
    telegram_id TEXT NOT NULL,
    user_message TEXT,
    tool_calls TEXT,
    outcome TEXT NOT NULL,
    reply TEXT,
    error TEXT,
    model_calls INTEGER NOT NULL DEFAULT 0,
    tool_call_count INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS csat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    poll_id TEXT NOT NULL UNIQUE,
    score INTEGER,
    answered_at TEXT,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.support_bot_db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        # CREATE TABLE IF NOT EXISTS won't add columns to a database created by
        # an earlier version, so bring old files forward explicitly.
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)")}
        if "draft_reply" not in existing:
            conn.execute("ALTER TABLE tickets ADD COLUMN draft_reply TEXT")
        if "forum_topic_id" not in existing:
            conn.execute("ALTER TABLE tickets ADD COLUMN forum_topic_id INTEGER")
        if "payment_expected" not in existing:
            conn.execute(
                "ALTER TABLE tickets ADD COLUMN payment_expected INTEGER NOT NULL DEFAULT 1"
            )
        if "live_chat" not in existing:
            conn.execute("ALTER TABLE tickets ADD COLUMN live_chat INTEGER NOT NULL DEFAULT 0")
        conversation_columns = {row["name"] for row in conn.execute("PRAGMA table_info(conversations)")}
        if conversation_columns and "expecting" not in conversation_columns:
            conn.execute(
                "ALTER TABLE conversations ADD COLUMN expecting TEXT NOT NULL DEFAULT 'text'"
            )
        if conversation_columns and "receipt" not in conversation_columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN receipt TEXT")


@dataclass
class TicketRecord:
    id: int
    telegram_id: str
    username: str | None
    contact: str | None
    problem_type: str
    apparat_name: str | None
    raw_text: str | None
    transaction_id: str | None
    status: str
    draft_reply: str | None
    forum_topic_id: int | None
    # False for complaints where no payment could have happened yet (the QR
    # never appeared, the bank refused, the file never uploaded). Asking those
    # users for a receipt reads as not having listened to them.
    payment_expected: int
    # While set, staff and the user are talking directly through the bot: every
    # message either side sends is relayed, with no buttons in between, until
    # staff close the ticket.
    live_chat: int
    created_at: str


def _create_ticket_sync(
    telegram_id: str,
    username: str | None,
    contact: str | None,
    problem_type: str,
    apparat_name: str | None,
    raw_text: str | None,
    payment_expected: bool,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO tickets (telegram_id, username, contact, problem_type, apparat_name, "
            "raw_text, status, payment_expected, created_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (
                telegram_id, username, contact, problem_type, apparat_name, raw_text,
                int(payment_expected), _now(),
            ),
        )
        return cur.lastrowid


async def create_ticket(
    telegram_id: str,
    username: str | None,
    contact: str | None,
    problem_type: str,
    apparat_name: str | None,
    raw_text: str | None,
    payment_expected: bool = True,
) -> int:
    return await asyncio.to_thread(
        _create_ticket_sync, telegram_id, username, contact, problem_type, apparat_name,
        raw_text, payment_expected,
    )


@dataclass
class Conversation:
    id: int
    telegram_id: str
    username: str | None
    status: str
    summary: str | None
    summarised_upto: int
    # What the agent asked the user for last, so a photo arriving next is read
    # as an answer to that question rather than as a stray file.
    expecting: str
    receipt: str | None
    created_at: str
    last_at: str


@dataclass
class ConversationMessage:
    id: int
    conversation_id: int
    role: str
    content: str | None
    tool_name: str | None
    tool_call_id: str | None
    tool_calls: str | None
    created_at: str


def _active_conversation_sync(telegram_id: str, ttl_minutes: int) -> Conversation | None:
    # The cutoff is computed here, against the same clock _now() writes with.
    # Working it out from tz.now() (naive Asia/Almaty) instead compares local
    # wall-clock against UTC rows and silently shortens the TTL by the offset.
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes)).isoformat()
    with _connect() as conn:
        row = conn.execute(
            """SELECT * FROM conversations
               WHERE telegram_id = ? AND status = 'open' AND last_at >= ?
               ORDER BY id DESC LIMIT 1""",
            (telegram_id, cutoff),
        ).fetchone()
        return Conversation(**dict(row)) if row else None


async def active_conversation(telegram_id: str, ttl_minutes: int) -> Conversation | None:
    return await asyncio.to_thread(_active_conversation_sync, telegram_id, ttl_minutes)


def _start_conversation_sync(telegram_id: str, username: str | None) -> int:
    now = _now()
    with _connect() as conn:
        # Whatever was open is over: one user has at most one live conversation,
        # so a stale one can never quietly collect new messages.
        conn.execute(
            "UPDATE conversations SET status = 'closed' WHERE telegram_id = ? AND status = 'open'",
            (telegram_id,),
        )
        cur = conn.execute(
            "INSERT INTO conversations (telegram_id, username, created_at, last_at) VALUES (?,?,?,?)",
            (telegram_id, username, now, now),
        )
        return int(cur.lastrowid)


async def start_conversation(telegram_id: str, username: str | None = None) -> int:
    return await asyncio.to_thread(_start_conversation_sync, telegram_id, username)


def _close_conversation_sync(conversation_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE conversations SET status = 'closed' WHERE id = ?", (conversation_id,))


async def close_conversation(conversation_id: int) -> None:
    await asyncio.to_thread(_close_conversation_sync, conversation_id)


def _append_message_sync(
    conversation_id: int,
    role: str,
    content: str | None,
    tool_name: str | None,
    tool_call_id: str | None,
    tool_calls: str | None,
) -> int:
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO conversation_messages
                   (conversation_id, role, content, tool_name, tool_call_id, tool_calls, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (conversation_id, role, content, tool_name, tool_call_id, tool_calls, now),
        )
        conn.execute("UPDATE conversations SET last_at = ? WHERE id = ?", (now, conversation_id))
        return int(cur.lastrowid)


async def append_message(
    conversation_id: int,
    role: str,
    content: str | None = None,
    tool_name: str | None = None,
    tool_call_id: str | None = None,
    tool_calls: str | None = None,
) -> int:
    return await asyncio.to_thread(
        _append_message_sync, conversation_id, role, content, tool_name, tool_call_id, tool_calls
    )


def _conversation_messages_sync(conversation_id: int, after_id: int) -> list[ConversationMessage]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM conversation_messages WHERE conversation_id = ? AND id > ? ORDER BY id",
            (conversation_id, after_id),
        ).fetchall()
        return [ConversationMessage(**dict(r)) for r in rows]


async def conversation_messages(conversation_id: int, after_id: int = 0) -> list[ConversationMessage]:
    return await asyncio.to_thread(_conversation_messages_sync, conversation_id, after_id)


def _set_conversation_summary_sync(conversation_id: int, summary: str, upto_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE conversations SET summary = ?, summarised_upto = ? WHERE id = ?",
            (summary, upto_id, conversation_id),
        )


async def set_conversation_summary(conversation_id: int, summary: str, upto_id: int) -> None:
    await asyncio.to_thread(_set_conversation_summary_sync, conversation_id, summary, upto_id)


def _get_conversation_sync(conversation_id: int) -> Conversation | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        return Conversation(**dict(row)) if row else None


async def get_conversation(conversation_id: int) -> Conversation | None:
    return await asyncio.to_thread(_get_conversation_sync, conversation_id)


def _save_buttons_sync(conversation_id: int, labels: list[str]) -> list[int]:
    now = _now()
    with _connect() as conn:
        ids = []
        for label in labels:
            cur = conn.execute(
                "INSERT INTO conversation_buttons (conversation_id, label, created_at) VALUES (?,?,?)",
                (conversation_id, label, now),
            )
            ids.append(int(cur.lastrowid))
        return ids


async def save_buttons(conversation_id: int, labels: list[str]) -> list[int]:
    return await asyncio.to_thread(_save_buttons_sync, conversation_id, labels)


def _get_button_sync(button_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM conversation_buttons WHERE id = ?", (button_id,)
        ).fetchone()
        return dict(row) if row else None


async def get_button(button_id: int) -> dict[str, Any] | None:
    return await asyncio.to_thread(_get_button_sync, button_id)


def _set_conversation_expecting_sync(conversation_id: int, expecting: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE conversations SET expecting = ? WHERE id = ?", (expecting, conversation_id)
        )


async def set_conversation_expecting(conversation_id: int, expecting: str) -> None:
    await asyncio.to_thread(_set_conversation_expecting_sync, conversation_id, expecting)


def _set_conversation_receipt_sync(conversation_id: int, receipt: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE conversations SET receipt = ? WHERE id = ?", (receipt, conversation_id)
        )


async def set_conversation_receipt(conversation_id: int, receipt: dict) -> None:
    await asyncio.to_thread(
        _set_conversation_receipt_sync, conversation_id, json.dumps(receipt, ensure_ascii=False, default=str)
    )


def _record_agent_turn_sync(
    telegram_id: str,
    outcome: str,
    user_message: str | None,
    reply: str | None,
    tool_calls: list[str] | None,
    error: str | None,
    model_calls: int,
    latency_ms: int | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    conversation_id: int | None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO agent_turns (
                   conversation_id, telegram_id, user_message, tool_calls, outcome, reply,
                   error, model_calls, tool_call_count, latency_ms, prompt_tokens,
                   completion_tokens, created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                conversation_id, telegram_id, user_message,
                json.dumps(tool_calls or [], ensure_ascii=False), outcome, reply, error,
                model_calls, len(tool_calls or []), latency_ms, prompt_tokens,
                completion_tokens, _now(),
            ),
        )


async def record_agent_turn(
    telegram_id: str,
    outcome: str,
    user_message: str | None = None,
    reply: str | None = None,
    tool_calls: list[str] | None = None,
    error: str | None = None,
    model_calls: int = 0,
    latency_ms: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    conversation_id: int | None = None,
) -> None:
    """Never let bookkeeping break a conversation: a failed write is logged and
    swallowed, because losing a metric is cheaper than losing the user's turn."""
    try:
        await asyncio.to_thread(
            _record_agent_turn_sync, telegram_id, outcome, user_message, reply, tool_calls,
            error, model_calls, latency_ms, prompt_tokens, completion_tokens, conversation_id,
        )
    except Exception:
        logger.exception("could not record agent turn for %s", telegram_id)


def _recent_agent_turns_sync(limit: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_turns ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


async def recent_agent_turns(limit: int = 20) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_recent_agent_turns_sync, limit)


def _get_ticket_sync(ticket_id: int) -> TicketRecord | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            return None
        return TicketRecord(**dict(row))


async def get_ticket(ticket_id: int) -> TicketRecord | None:
    return await asyncio.to_thread(_get_ticket_sync, ticket_id)


def _set_ticket_transaction_sync(ticket_id: int, transaction_id: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE tickets SET transaction_id = ? WHERE id = ?", (transaction_id, ticket_id))


async def set_ticket_transaction(ticket_id: int, transaction_id: str) -> None:
    await asyncio.to_thread(_set_ticket_transaction_sync, ticket_id, transaction_id)


def _set_ticket_status_sync(ticket_id: int, status: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE tickets SET status = ? WHERE id = ?", (status, ticket_id))


async def set_ticket_status(ticket_id: int, status: str) -> None:
    await asyncio.to_thread(_set_ticket_status_sync, ticket_id, status)


def _set_ticket_contact_sync(ticket_id: int, contact: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE tickets SET contact = ? WHERE id = ?", (contact, ticket_id))


async def set_ticket_contact(ticket_id: int, contact: str) -> None:
    await asyncio.to_thread(_set_ticket_contact_sync, ticket_id, contact)


def _set_ticket_draft_reply_sync(ticket_id: int, draft_reply: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE tickets SET draft_reply = ? WHERE id = ?", (draft_reply, ticket_id))


async def set_ticket_draft_reply(ticket_id: int, draft_reply: str) -> None:
    """The message the AI prepared for the user, sent only if staff approve."""
    await asyncio.to_thread(_set_ticket_draft_reply_sync, ticket_id, draft_reply)


def _set_ticket_topic_sync(ticket_id: int, forum_topic_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE tickets SET forum_topic_id = ? WHERE id = ?", (forum_topic_id, ticket_id)
        )


async def set_ticket_topic(ticket_id: int, forum_topic_id: int) -> None:
    await asyncio.to_thread(_set_ticket_topic_sync, ticket_id, forum_topic_id)


def _get_ticket_by_topic_sync(forum_topic_id: int) -> TicketRecord | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM tickets WHERE forum_topic_id = ? ORDER BY id DESC LIMIT 1",
            (forum_topic_id,),
        ).fetchone()
        return TicketRecord(**dict(row)) if row else None


async def get_ticket_by_topic(forum_topic_id: int) -> TicketRecord | None:
    """Maps a staff message in a forum thread back to the ticket it belongs to."""
    return await asyncio.to_thread(_get_ticket_by_topic_sync, forum_topic_id)


def _find_ticket_awaiting_receipt_sync(telegram_id: str) -> TicketRecord | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM tickets WHERE telegram_id = ? AND status = 'awaiting_receipt' "
            "ORDER BY id DESC LIMIT 1",
            (telegram_id,),
        ).fetchone()
        return TicketRecord(**dict(row)) if row else None


def _set_live_chat_sync(ticket_id: int, on: bool) -> None:
    with _connect() as conn:
        conn.execute("UPDATE tickets SET live_chat = ? WHERE id = ?", (int(on), ticket_id))


async def set_live_chat(ticket_id: int, on: bool) -> None:
    await asyncio.to_thread(_set_live_chat_sync, ticket_id, on)


def _find_live_chat_ticket_sync(telegram_id: str) -> TicketRecord | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM tickets WHERE telegram_id = ? AND live_chat = 1 "
            "ORDER BY id DESC LIMIT 1",
            (telegram_id,),
        ).fetchone()
        return TicketRecord(**dict(row)) if row else None


async def find_live_chat_ticket(telegram_id: str) -> TicketRecord | None:
    """Anything this user sends goes to the staff thread while this is open,
    instead of to the assistant."""
    return await asyncio.to_thread(_find_live_chat_ticket_sync, telegram_id)


async def find_ticket_awaiting_receipt(telegram_id: str) -> TicketRecord | None:
    """Staff asked this user for a receipt, possibly hours ago and long after
    their chat session was cleared - this is how an out-of-the-blue photo gets
    attached to the right ticket."""
    return await asyncio.to_thread(_find_ticket_awaiting_receipt_sync, telegram_id)


def _record_decision_sync(
    ticket_id: int,
    evidence: dict[str, Any],
    ai_action: str | None,
    ai_reasoning: str | None,
    guard_triggered: str | None,
    final_action: str,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO decisions (ticket_id, evidence_json, ai_action, ai_reasoning, "
            "guard_triggered, final_action, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ticket_id,
                json.dumps(evidence, default=str, ensure_ascii=False),
                ai_action,
                ai_reasoning,
                guard_triggered,
                final_action,
                _now(),
            ),
        )
        return cur.lastrowid


async def record_decision(
    ticket_id: int,
    evidence: dict[str, Any],
    ai_action: str | None,
    ai_reasoning: str | None,
    guard_triggered: str | None,
    final_action: str,
) -> int:
    return await asyncio.to_thread(
        _record_decision_sync, ticket_id, evidence, ai_action, ai_reasoning, guard_triggered, final_action
    )


def _was_already_refunded_sync(transaction_id: str) -> bool:
    # Refunds are only ever recorded by a staff confirmation in notify.py, which
    # writes the transaction_id into evidence_json so this lookup can find it.
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM decisions WHERE final_action = 'refund_confirmed' AND "
            "json_extract(evidence_json, '$.transaction_id') = ? LIMIT 1",
            (transaction_id,),
        ).fetchone()
        return row is not None


async def was_already_refunded(transaction_id: str) -> bool:
    return await asyncio.to_thread(_was_already_refunded_sync, transaction_id)


def _create_escalation_sync(ticket_id: int, staff_chat_id: int, message_id: int) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO escalations (ticket_id, staff_chat_id, message_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (ticket_id, staff_chat_id, message_id, _now()),
        )
        return cur.lastrowid


async def create_escalation(ticket_id: int, staff_chat_id: int, message_id: int) -> int:
    return await asyncio.to_thread(_create_escalation_sync, ticket_id, staff_chat_id, message_id)


def _resolve_escalation_sync(message_id: int, resolved_by: str, resolution: str) -> int | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, ticket_id FROM escalations WHERE message_id = ?", (message_id,)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE escalations SET resolved_by = ?, resolution = ?, resolved_at = ? WHERE id = ?",
            (resolved_by, resolution, _now(), row["id"]),
        )
        return row["ticket_id"]


async def resolve_escalation(message_id: int, resolved_by: str, resolution: str) -> int | None:
    return await asyncio.to_thread(_resolve_escalation_sync, message_id, resolved_by, resolution)


def _create_csat_sync(ticket_id: int, poll_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO csat (ticket_id, poll_id, created_at) VALUES (?, ?, ?)",
            (ticket_id, poll_id, _now()),
        )


async def create_csat(ticket_id: int, poll_id: str) -> None:
    await asyncio.to_thread(_create_csat_sync, ticket_id, poll_id)


def _record_csat_score_sync(poll_id: str, score: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE csat SET score = ?, answered_at = ? WHERE poll_id = ?",
            (score, _now(), poll_id),
        )


async def record_csat_score(poll_id: str, score: int) -> None:
    """Score is the index of the chosen option: 0 is the best outcome."""
    await asyncio.to_thread(_record_csat_score_sync, poll_id, score)
