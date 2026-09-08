"""The bot may only claim a hand-off when a hand-off actually happens.

give_advice, ask_clarifying_question and a follow-up resolve all notify nobody -
a reply from any of them saying "передал сотрудникам" leaves the user waiting
for an answer that will never come. The prompts forbid it; these tests pin the
code-level backstop that catches it when a model slips anyway.
"""
import ai_decider


def test_a_russian_promise_is_recognised():
    assert ai_decider.promises_a_handoff("Я передал информацию сотруднику поддержки.")
    assert ai_decider.promises_a_handoff("Сообщу нашим специалистам, они проверят аппарат.")
    assert ai_decider.promises_a_handoff("Передаю обращение нашей команде.")


def test_a_kazakh_promise_is_recognised():
    assert ai_decider.promises_a_handoff(
        "Мен бұл мәселені қызметкерлерімізге хабарлаймын, олар тексереді."
    )


def test_plain_advice_is_not_mistaken_for_a_promise():
    assert not ai_decider.promises_a_handoff(
        "Попробуйте отправить файл заново — код придёт сразу после загрузки."
    )
    assert not ai_decider.promises_a_handoff("Сотрудники обслуживают аппараты сами.")
    assert not ai_decider.promises_a_handoff(None)


def _tool_call(name, args):
    from types import SimpleNamespace
    import json

    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[
            SimpleNamespace(function=SimpleNamespace(name=name, arguments=json.dumps(args)))
        ]))]
    )


async def test_advice_that_promises_staff_becomes_a_real_escalation(monkeypatch):
    from diagnosis import Evidence, TicketInput
    import tz

    response = _tool_call("give_advice", {"text": "Я передам ваше обращение сотруднику поддержки."})

    async def _call(fn):
        return response

    monkeypatch.setattr(ai_decider.openai_utils, "call_with_one_retry", _call)
    monkeypatch.setattr(ai_decider, "_client", lambda: None)

    evidence = Evidence(
        ticket=TicketInput(
            problem_type="not_printed", apparat_name_text="Аппарат №3", telegram_id="1",
            username="u", contact=None, raw_text="не вышло", submitted_at=tz.now(),
        ),
        identity_confirmed=True, transaction=None, apparat=None,
    )
    decision = await ai_decider.decide(evidence)
    assert decision.action == "escalate"
    assert decision.user_message  # the user still gets the text they were promised
    assert decision.staff_summary


async def test_a_followup_promise_escalates_when_nobody_has_the_ticket(monkeypatch):
    response = _tool_call("resolve", {"reply_text": "Передам сотрудникам, они посмотрят аппарат."})

    async def _call(fn):
        return response

    monkeypatch.setattr(ai_decider.openai_utils, "call_with_one_retry", _call)
    monkeypatch.setattr(ai_decider, "_client", lambda: None)

    decision = await ai_decider.decide_followup(
        original_reply="…", problem_type="print_quality", raw_text="бледно",
        user_followup="всё ещё плохо", already_with_staff=False,
    )
    assert decision.action == "escalate"


async def test_the_same_promise_stands_when_staff_already_have_it(monkeypatch):
    # The quality flow escalates as soon as the complaint arrives, so by the
    # time the user taps "не помогло" staff really are on it - saying so is
    # then the truth, not a brush-off.
    response = _tool_call("resolve", {"reply_text": "Сотрудники уже проверяют этот аппарат."})

    async def _call(fn):
        return response

    monkeypatch.setattr(ai_decider.openai_utils, "call_with_one_retry", _call)
    monkeypatch.setattr(ai_decider, "_client", lambda: None)

    decision = await ai_decider.decide_followup(
        original_reply="…", problem_type="print_quality", raw_text="бледно",
        user_followup="всё ещё плохо", already_with_staff=True,
    )
    assert decision.action == "resolve"


def test_the_service_overview_states_the_real_working_hours():
    # A model told a user the kiosks run 24/7 because nothing said otherwise.
    import advice

    assert "8:00" in advice.SERVICE_OVERVIEW and "19:00" in advice.SERVICE_OVERVIEW


def test_every_user_facing_prompt_knows_how_the_service_works():
    # decide() answered "работаете круглосуточно?" wrongly for exactly one
    # reason: it was the only user-facing prompt without the overview.
    import ai_decider
    import concierge

    for name, prompt in [
        ("decide", ai_decider._SYSTEM_PROMPT),
        ("decide_followup", ai_decider._FOLLOWUP_SYSTEM_PROMPT),
        ("concierge", concierge._SYSTEM_PROMPT),
    ]:
        assert "8:00" in prompt and "19:00" in prompt, name
