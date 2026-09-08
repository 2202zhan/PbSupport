"""Phase 2: one turn of the agent.

The invariant that matters most: a turn always ends with something the user can
see. Out of budget, timed out, model down, bad arguments - whatever happens,
either an answer goes out or a human is called in, never silence.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

import storage
from agent import memory, runtime
from agent.tools import DEFAULT_TOOLS, ToolError, ToolRegistry, ToolSpec
from agent.types import TurnContext, TurnResult


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage.settings, "support_bot_db_path", str(tmp_path / "t.sqlite3"))
    storage.init_db()


def _tool_call(name, args, call_id="c1"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)),
        model_dump=lambda: {
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        },
    )


def _response(tool_calls=None, content=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=tool_calls, content=content))]
    )


def _model(monkeypatch, *responses):
    """Replays the given responses, one per model call."""
    queue = list(responses)
    seen = []

    async def _call(make_call):
        seen.append(True)
        if not queue:
            raise AssertionError("model called more times than the test expected")
        return queue.pop(0)

    monkeypatch.setattr(runtime.openai_utils, "call_with_one_retry", _call)
    monkeypatch.setattr(runtime, "_client", lambda: None)
    return seen


async def _ctx(text="не распечатал"):
    conversation = await memory.current_conversation("884013433", "zhan")
    await memory.remember_user_message(conversation.id, text)
    return TurnContext(
        telegram_id="884013433", username="zhan",
        conversation_id=conversation.id, user_message=text,
    )


async def test_a_plain_answer_ends_the_turn(db, monkeypatch):
    _model(monkeypatch, _response([_tool_call("reply", {"text": "Мы работаем с 8:00 до 19:00."})]))
    result = await runtime.run_turn(await _ctx("во сколько работаете?"))
    assert result.kind == "reply"
    assert "8:00" in result.text
    assert result.tool_calls == ["reply"]


async def test_calling_for_a_human_carries_a_summary(db, monkeypatch):
    _model(monkeypatch, _response([_tool_call("escalate", {
        "reason": "просит оператора",
        "summary_for_staff": "Юзер просит живого человека по поводу возврата.",
        "message_for_user": "Передал сотруднику, он ответит здесь.",
    })]))
    result = await runtime.run_turn(await _ctx("позовите оператора"))
    assert result.kind == "escalate"
    assert result.needs_staff
    assert "возврата" in result.staff_summary
    assert result.text


async def test_a_promise_of_staff_becomes_a_real_escalation(db, monkeypatch):
    # reply notifies nobody, so a reply that says it did has to become true.
    _model(monkeypatch, _response([_tool_call("reply", {
        "text": "Я передал ваше обращение сотрудникам, они посмотрят аппарат."
    })]))
    result = await runtime.run_turn(await _ctx("аппарат сломан"))
    assert result.kind == "escalate"
    assert result.staff_summary


async def test_bad_arguments_are_handed_back_to_the_model(db, monkeypatch):
    # A missing argument costs one more model call, not the whole turn.
    _model(
        monkeypatch,
        _response([_tool_call("reply", {})]),
        _response([_tool_call("reply", {"text": "Готово."}, call_id="c2")]),
    )
    result = await runtime.run_turn(await _ctx())
    assert result.kind == "reply" and result.text == "Готово."
    assert result.model_calls == 2


async def test_an_unknown_tool_does_not_end_the_turn(db, monkeypatch):
    _model(
        monkeypatch,
        _response([_tool_call("refund_money", {"amount": 100})]),
        _response([_tool_call("reply", {"text": "Возврат подтверждает сотрудник."}, call_id="c2")]),
    )
    result = await runtime.run_turn(await _ctx("верни деньги"))
    assert result.kind == "reply"


async def test_prose_instead_of_a_tool_call_is_still_delivered(db, monkeypatch):
    # Making the user wait for a retry is worse than sending what it wrote.
    _model(monkeypatch, _response(tool_calls=None, content="Мы работаем с 8 до 19."))
    result = await runtime.run_turn(await _ctx())
    assert result.kind == "reply" and "8 до 19" in result.text


async def test_a_model_that_says_nothing_at_all_calls_a_human(db, monkeypatch):
    _model(monkeypatch, _response(tool_calls=None, content=""))
    result = await runtime.run_turn(await _ctx())
    assert result.needs_staff and result.text


async def test_running_out_of_model_calls_calls_a_human(db, monkeypatch):
    monkeypatch.setattr(runtime.settings, "agent_max_model_calls", 2)
    data = ToolSpec(
        name="look", description="", parameters={"type": "object", "properties": {}},
        terminal=False, run=lambda _a, _c: _ok(),
    )
    _model(
        monkeypatch,
        _response([_tool_call("look", {})]),
        _response([_tool_call("look", {}, call_id="c2")]),
    )
    result = await runtime.run_turn(await _ctx(), ToolRegistry([data]))
    assert result.kind == "failed"
    assert result.text and result.staff_summary
    assert result.error == "model call budget exhausted"


async def _ok():
    return {"ответ": "ок"}


async def test_running_out_of_tool_calls_calls_a_human(db, monkeypatch):
    monkeypatch.setattr(runtime.settings, "agent_max_tool_calls", 2)
    data = ToolSpec(
        name="look", description="", parameters={"type": "object", "properties": {}},
        terminal=False, run=lambda _a, _c: _ok(),
    )
    _model(monkeypatch, _response([
        _tool_call("look", {}, "c1"), _tool_call("look", {}, "c2"), _tool_call("look", {}, "c3"),
    ]))
    result = await runtime.run_turn(await _ctx(), ToolRegistry([data]))
    assert result.kind == "failed" and result.error == "tool budget exhausted"


async def test_a_hanging_model_does_not_hang_the_user(db, monkeypatch):
    monkeypatch.setattr(runtime.settings, "agent_turn_timeout_seconds", 0.05)

    async def _forever(_make_call):
        await asyncio.sleep(5)

    monkeypatch.setattr(runtime.openai_utils, "call_with_one_retry", _forever)
    monkeypatch.setattr(runtime, "_client", lambda: None)
    result = await runtime.run_turn(await _ctx())
    assert result.kind == "failed" and result.error == "timeout"
    assert result.text


async def test_a_broken_model_call_calls_a_human(db, monkeypatch):
    async def _boom(_make_call):
        raise RuntimeError("provider down")

    monkeypatch.setattr(runtime.openai_utils, "call_with_one_retry", _boom)
    monkeypatch.setattr(runtime, "_client", lambda: None)
    result = await runtime.run_turn(await _ctx())
    assert result.kind == "failed" and "provider down" in result.error
    assert result.text


async def test_the_exchange_is_remembered_for_the_next_turn(db, monkeypatch):
    _model(monkeypatch, _response([_tool_call("reply", {"text": "Проверю."})]))
    ctx = await _ctx("бледно печатает")
    await runtime.run_turn(ctx)
    replay = await memory.history(ctx.conversation_id)
    assert [m["role"] for m in replay] == ["user", "assistant", "tool"]
    assert replay[1]["tool_calls"][0]["function"]["name"] == "reply"


async def test_a_data_tool_feeds_the_model_and_the_loop_continues(db, monkeypatch):
    seen = {}

    async def _look(args, _ctx):
        seen["args"] = args
        return {"бумага": "есть", "тонер": "достаточно"}

    data = ToolSpec(
        name="check_apparat", description="",
        parameters={"type": "object", "properties": {"place": {"type": "string"}}},
        terminal=False, run=_look,
    )
    _model(
        monkeypatch,
        _response([_tool_call("check_apparat", {"place": "главный корпус"})]),
        _response([_tool_call("reply", {"text": "Расходники на месте."}, call_id="c2")]),
    )
    from agent.tools import REPLY

    ctx = await _ctx()
    result = await runtime.run_turn(ctx, ToolRegistry([data, REPLY]))
    assert seen["args"] == {"place": "главный корпус"}
    assert result.kind == "reply"
    assert result.tool_calls == ["check_apparat", "reply"]
    replay = await memory.history(ctx.conversation_id)
    assert any(m["role"] == "tool" and "тонер" in m["content"] for m in replay)


async def test_every_turn_leaves_a_row_to_look_at_later(db, monkeypatch):
    _model(monkeypatch, _response([_tool_call("reply", {"text": "Ответ."})]))
    ctx = await _ctx("вопрос")
    await runtime.run_turn(ctx)
    (row,) = await storage.recent_agent_turns()
    assert row["outcome"] == "reply"
    assert row["conversation_id"] == ctx.conversation_id
    assert row["latency_ms"] is not None
    assert json.loads(row["tool_calls"]) == ["reply"]


def test_only_the_talking_tools_end_the_turn():
    # Reading tools feed the model and the loop continues; only reply and
    # escalate finish a turn.
    assert {s["function"]["name"] for s in DEFAULT_TOOLS.schemas} == {
        "service_info", "check_apparat", "find_my_orders", "reply", "escalate",
    }
    assert DEFAULT_TOOLS.get("reply").terminal
    assert DEFAULT_TOOLS.get("escalate").terminal
    for name in ("service_info", "check_apparat", "find_my_orders"):
        assert not DEFAULT_TOOLS.get(name).terminal


async def test_empty_reply_text_is_rejected_not_sent():
    with pytest.raises(ToolError):
        await DEFAULT_TOOLS.get("reply").run({"text": "   "}, None)


async def test_escalation_without_a_summary_is_rejected():
    with pytest.raises(ToolError):
        await DEFAULT_TOOLS.get("escalate").run({"reason": "просто"}, None)


def test_a_failed_turn_counts_as_needing_staff():
    assert TurnResult(kind="failed").needs_staff
    assert TurnResult(kind="escalate").needs_staff
    assert not TurnResult(kind="reply").needs_staff
