import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import ai_decider
import tz
from diagnosis import Evidence, TicketInput


def _evidence() -> Evidence:
    ticket = TicketInput(
        problem_type="not_printed",
        apparat_name_text="Аппарат №3",
        telegram_id="123",
        username="user",
        contact=None,
        raw_text="не вышла распечатка, а деньги списали!",
        submitted_at=tz.now(),
        dialogue_history=["user: не вышла распечатка, а деньги списали!"],
    )
    return Evidence(ticket=ticket, identity_confirmed=True, transaction=None, apparat=None)


def _fake_response(tool_name: str | None, arguments: dict | None, raw_arguments: str | None = None):
    if tool_name is None:
        message = SimpleNamespace(tool_calls=None)
    else:
        args_str = raw_arguments if raw_arguments is not None else json.dumps(arguments, ensure_ascii=False)
        call = SimpleNamespace(function=SimpleNamespace(name=tool_name, arguments=args_str))
        message = SimpleNamespace(tool_calls=[call])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _patched_client(response=None, side_effect=None):
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(return_value=response, side_effect=side_effect)
            )
        )
    )
    return patch("ai_decider._client", return_value=fake_client)


async def test_auto_refund_tool_call_maps_to_decision():
    response = _fake_response("auto_refund", {"reason": "clear technical failure, no SNMP printing event"})
    with _patched_client(response=response):
        decision = await ai_decider.decide(_evidence())
    assert decision.action == "auto_refund"
    assert "technical failure" in decision.reason


async def test_give_advice_tool_call_maps_to_decision():
    response = _fake_response("give_advice", {"text": "Попробуйте отправить файл ещё раз как документ."})
    with _patched_client(response=response):
        decision = await ai_decider.decide(_evidence())
    assert decision.action == "give_advice"
    assert decision.user_message.startswith("Попробуйте")


async def test_ask_clarifying_question_tool_call_maps_to_decision():
    response = _fake_response("ask_clarifying_question", {"question": "Когда примерно это было?"})
    with _patched_client(response=response):
        decision = await ai_decider.decide(_evidence())
    assert decision.action == "ask_clarifying_question"
    assert decision.user_message == "Когда примерно это было?"


async def test_escalate_tool_call_maps_to_decision():
    response = _fake_response(
        "escalate", {"reason": "signal contradicts complaint", "summary_for_staff": "needs physical check"}
    )
    with _patched_client(response=response):
        decision = await ai_decider.decide(_evidence())
    assert decision.action == "escalate"
    assert decision.staff_summary == "needs physical check"


async def test_no_tool_call_falls_back_to_escalate():
    response = _fake_response(None, None)
    with _patched_client(response=response):
        decision = await ai_decider.decide(_evidence())
    assert decision.action == "escalate"
    assert decision.reason == "ai_decider_failed"


async def test_invalid_json_arguments_falls_back_to_escalate():
    response = _fake_response("auto_refund", None, raw_arguments="{not valid json")
    with _patched_client(response=response):
        decision = await ai_decider.decide(_evidence())
    assert decision.action == "escalate"
    assert decision.reason == "ai_decider_failed"


async def test_api_exception_falls_back_to_escalate():
    with _patched_client(side_effect=RuntimeError("network down")):
        decision = await ai_decider.decide(_evidence())
    assert decision.action == "escalate"
    assert decision.reason == "ai_decider_failed"


async def test_missing_required_argument_falls_back_to_escalate():
    response = _fake_response("escalate", {"reason": "x"})  # missing summary_for_staff
    with _patched_client(response=response):
        decision = await ai_decider.decide(_evidence())
    assert decision.action == "escalate"
    assert decision.reason == "ai_decider_failed"


async def test_followup_resolve_tool_call_maps_to_decision():
    response = _fake_response("resolve", {"reply_text": "Понимаю, попробуйте обновить страницу и повторить."})
    with _patched_client(response=response):
        decision = await ai_decider.decide_followup(
            original_reply="Попробуйте ещё раз.",
            problem_type="payment_error",
            raw_text="QR-код не появился на экране",
            user_followup="я не понял что делать",
        )
    assert decision.action == "resolve"
    assert decision.reply_text.startswith("Понимаю")


async def test_followup_escalate_tool_call_maps_to_decision():
    response = _fake_response(
        "escalate", {"reason": "explicit refund request", "summary_for_staff": "юзер требует возврат денег"}
    )
    with _patched_client(response=response):
        decision = await ai_decider.decide_followup(
            original_reply="Похоже на разовый сбой.",
            problem_type="payment_error",
            raw_text="деньги списались",
            user_followup="хочу вернуть деньги, это не разовый сбой",
        )
    assert decision.action == "escalate"
    assert decision.staff_summary == "юзер требует возврат денег"


async def test_followup_no_tool_call_falls_back_to_escalate():
    response = _fake_response(None, None)
    with _patched_client(response=response):
        decision = await ai_decider.decide_followup(
            original_reply="...", problem_type="other", raw_text="...", user_followup="..."
        )
    assert decision.action == "escalate"
    assert decision.reason == "followup_decider_failed"


async def test_followup_api_exception_falls_back_to_escalate():
    with _patched_client(side_effect=RuntimeError("network down")):
        decision = await ai_decider.decide_followup(
            original_reply="...", problem_type="other", raw_text="...", user_followup="..."
        )
    assert decision.action == "escalate"
    assert decision.reason == "followup_decider_failed"


async def test_followup_missing_required_argument_falls_back_to_escalate():
    response = _fake_response("escalate", {"reason": "x"})  # missing summary_for_staff
    with _patched_client(response=response):
        decision = await ai_decider.decide_followup(
            original_reply="...", problem_type="other", raw_text="...", user_followup="..."
        )
    assert decision.action == "escalate"
    assert decision.reason == "followup_decider_failed"
