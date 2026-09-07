from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import csat
import storage


def _bot_returning_poll(poll_id: str | None = "poll-1") -> AsyncMock:
    bot = AsyncMock()
    poll = SimpleNamespace(id=poll_id) if poll_id else None
    bot.send_poll = AsyncMock(return_value=SimpleNamespace(poll=poll))
    return bot


async def test_poll_is_recorded_against_the_ticket():
    bot = _bot_returning_poll()
    with patch.object(storage, "create_csat", AsyncMock()) as create_mock:
        await csat.send_poll(bot, chat_id=123, ticket_id=7)

    bot.send_poll.assert_called_once()
    create_mock.assert_called_once_with(7, "poll-1")


async def test_failed_poll_never_raises():
    # Closing a ticket must not depend on the survey going out.
    bot = AsyncMock()
    bot.send_poll = AsyncMock(side_effect=RuntimeError("chat not found"))

    await csat.send_poll(bot, chat_id=123, ticket_id=7)


async def test_failed_write_never_raises():
    bot = _bot_returning_poll()
    with patch.object(storage, "create_csat", AsyncMock(side_effect=RuntimeError("db locked"))):
        await csat.send_poll(bot, chat_id=123, ticket_id=7)


async def test_answer_stores_the_chosen_option():
    answer = SimpleNamespace(poll_id="poll-1", option_ids=[0])
    with patch.object(storage, "record_csat_score", AsyncMock()) as score_mock:
        await csat.on_poll_answer(answer)

    score_mock.assert_called_once_with("poll-1", 0)


async def test_retracted_vote_is_ignored():
    # Telegram sends an empty option list when someone takes their vote back.
    answer = SimpleNamespace(poll_id="poll-1", option_ids=[])
    with patch.object(storage, "record_csat_score", AsyncMock()) as score_mock:
        await csat.on_poll_answer(answer)

    score_mock.assert_not_called()


def test_best_outcome_is_the_first_option():
    # The stored score is the option index, so the ordering is the scale.
    assert csat._OPTIONS[0].startswith("👍")
    assert csat._OPTIONS[-1].startswith("👎")
