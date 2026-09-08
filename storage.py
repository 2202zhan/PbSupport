import asyncio
import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
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
