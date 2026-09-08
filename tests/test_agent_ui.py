"""Phase 3: the buttons are drawn per message, not fixed in the code.

This is the point of the rewrite - "бледная печать" and "не распечатал" must
lead to different questions with different answers under them - so what these
tests pin is that nothing here is a menu.
"""
import pytest

import storage
from agent import memory, ui
from agent.tools import DEFAULT_TOOLS, MAX_BUTTONS, ToolError, sanitize_buttons


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage.settings, "support_bot_db_path", str(tmp_path / "t.sqlite3"))
    storage.init_db()


def test_the_agent_gets_to_choose_the_labels():
    assert sanitize_buttons(["только что", "час назад", "вчера"]) == [
        "только что", "час назад", "вчера"
    ]


def test_junk_is_dropped_rather_than_failing_the_turn():
    # The message still stands on its own without its buttons, so a malformed
    # one is not worth costing the user an answer.
    assert sanitize_buttons(["  бледно  ", "", None, 42, "бледно"]) == ["бледно"]


def test_too_many_buttons_are_cut_not_rejected():
    assert len(sanitize_buttons([f"кнопка {i}" for i in range(10)])) == MAX_BUTTONS


def test_a_long_label_is_trimmed_to_fit():
    (label,) = sanitize_buttons(["очень длинная подпись " * 10])
    assert len(label) <= 40


def test_buttons_that_are_not_a_list_are_worth_correcting():
    with pytest.raises(ToolError):
        sanitize_buttons("бледно, полосы")


async def test_a_reply_carries_the_buttons_it_drew():
    result = await DEFAULT_TOOLS.get("reply").run(
        {"text": "Когда это было?", "buttons": ["только что", "вчера"], "expect": "choice"}, None
    )
    assert result.kind == "reply"
    assert result.buttons == ["только что", "вчера"]
    assert result.expect == "choice"


async def test_a_plain_answer_needs_no_buttons():
    result = await DEFAULT_TOOLS.get("reply").run({"text": "Работаем с 8 до 19."}, None)
    assert result.buttons == []
    assert result.expect == "text"


async def test_buttons_without_an_expectation_mean_a_choice():
    result = await DEFAULT_TOOLS.get("reply").run(
        {"text": "Что именно не так?", "buttons": ["бледно", "полосы"]}, None
    )
    assert result.expect == "choice"


async def test_a_nonsense_expectation_falls_back(db):
    result = await DEFAULT_TOOLS.get("reply").run(
        {"text": "Пришлите чек", "expect": "телепатия"}, None
    )
    assert result.expect == "text"


async def test_every_message_offers_a_way_to_a_human(db):
    # Someone who has stopped trusting the bot should not have to phrase a
    # request to escape it.
    conversation = await memory.current_conversation("1")
    markup = await ui.keyboard(conversation.id, [])
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == [ui.CALL_HUMAN]


async def test_the_agents_own_buttons_come_first(db):
    conversation = await memory.current_conversation("1")
    markup = await ui.keyboard(conversation.id, ["бледно", "полосы"])
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == ["бледно", "полосы", ui.CALL_HUMAN]


async def test_a_tap_comes_back_as_what_the_button_said(db):
    conversation = await memory.current_conversation("1")
    markup = await ui.keyboard(conversation.id, ["только что"])
    data = markup.inline_keyboard[0][0].callback_data
    assert len(data.encode()) <= 64  # Telegram's limit
    assert await ui.resolve(data, conversation.id) == "только что"


async def test_a_button_from_another_conversation_is_not_honoured(db):
    # Tapping something from a closed case must not quietly reopen it.
    mine = await memory.current_conversation("1")
    markup = await ui.keyboard(mine.id, ["да"])
    data = markup.inline_keyboard[0][0].callback_data

    theirs = await memory.current_conversation("2")
    assert await ui.resolve(data, theirs.id) is None


async def test_a_malformed_callback_is_ignored(db):
    conversation = await memory.current_conversation("1")
    assert await ui.resolve("ab:не-число", conversation.id) is None
    assert await ui.resolve("ab:999999", conversation.id) is None
