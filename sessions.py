"""Tracks which chats are mid-conversation waiting on a reply from the user, so
an inactivity timeout can close stale sessions (FAQ left open, ticket
description never sent, AI clarifying question left unanswered). Tickets that
have already moved to investigation/escalation are `forget()`-ten - we're not
waiting on the user any more, so they should never time out.
"""

from datetime import datetime, timedelta

import tz

_last_activity: dict[str, datetime] = {}


def touch(telegram_id: str) -> None:
    _last_activity[telegram_id] = tz.now()


def forget(telegram_id: str) -> None:
    _last_activity.pop(telegram_id, None)


def pop_expired(timeout_minutes: int) -> list[str]:
    cutoff = tz.now() - timedelta(minutes=timeout_minutes)
    expired = [tg_id for tg_id, last in _last_activity.items() if last <= cutoff]
    for tg_id in expired:
        _last_activity.pop(tg_id, None)
    return expired
