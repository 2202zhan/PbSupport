"""What the agent is not allowed to say, checked in code.

These are the two rules a prompt is worst at holding, and both were broken in
production by the menu bot before they were enforced here: promising a hand-off
that never happens, and telling someone standing at our kiosk to service it.
"""
import pytest

from agent import guards
from agent.tools import DEFAULT_TOOLS, ToolError


@pytest.mark.parametrize("text", [
    "Попробуйте перезапустить киоск или подождать пару минут.",
    "Перезагрузите аппарат и попробуйте снова.",
    "Замените картридж — тонер на исходе.",
    "Почистите ролики внутри принтера.",
    "Досыпьте бумагу в лоток.",
    "Откройте крышку аппарата и достаньте лист.",
    "Встряхните картридж, это поможет.",
])
def test_advice_the_user_cannot_and_must_not_follow_is_caught(text):
    assert guards.self_service_advice(text)


@pytest.mark.parametrize("text", [
    # Our own work, described in the first person - not an instruction.
    "Мы заменим картридж сами, это наша задача.",
    "Перезагрузим аппарат со своей стороны.",
    # The user's own device is fair game.
    "Перезагрузите телеграм и отправьте файл заново.",
    "Попробуйте распечатать ещё раз или на другом нашем аппарате.",
    "Пришлите, пожалуйста, чек — посмотрю по нашим данным.",
])
def test_legitimate_advice_is_left_alone(text):
    assert guards.self_service_advice(text) is None


def test_nothing_to_check_is_not_a_violation():
    assert guards.self_service_advice(None) is None
    assert guards.self_service_advice("") is None


@pytest.mark.parametrize("text", [
    "Я вызову нашего сотрудника для обслуживания аппарата.",
    "Мы пришлём кого-нибудь для решения.",
    "Передам информацию специалистам.",
    "Мен қызметкерлерімізге хабарлаймын.",
])
def test_a_promise_of_a_human_is_caught_however_it_is_phrased(text):
    assert guards.promises_a_handoff(text)


async def test_self_service_advice_is_sent_back_for_a_rewrite():
    # Recoverable on purpose: the rest of the answer may be fine, so the model
    # gets told what to fix instead of the user losing the turn.
    with pytest.raises(ToolError) as caught:
        await DEFAULT_TOOLS.get("reply").run(
            {"text": "Попробуйте перезагрузить аппарат."}, None
        )
    assert "перепиши" in str(caught.value).lower()


async def test_a_promise_of_a_human_becomes_a_real_escalation():
    # Not recoverable, and shouldn't be: what it said is fixable by making it
    # true, which is better for the user than a reworded brush-off.
    result = await DEFAULT_TOOLS.get("reply").run(
        {"text": "Я вызову сотрудника, он посмотрит аппарат."}, None
    )
    assert result.kind == "escalate" and result.staff_summary
