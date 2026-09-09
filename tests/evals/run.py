"""Runs the scenario suite and reports what held and what didn't.

Usable two ways: `python -m tests.evals.run` for a readable report, and from
test_evals.py so a red run blocks the switch-over.
"""

import asyncio
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime

import apparats
import diagnosis
import storage
import tz
from agent import guards, memory, reading
from agent.runtime import run_turn
from agent.types import TurnContext
from tests.evals.cases import CASES, Case
from tests.evals.world import FakeApi

# Anything here in a message to the user is our plumbing showing through.
_INTERNALS = re.compile(
    r"\bsnmp\b|\bлог(?:ах|и|ов|у|е)?\b|мониторинг|счётчик|счетчик|\bapi\b|эндпоинт|"
    r"транзакц|telegram_id|print_signal|evidence|guard_rules|investigate_order|"
    r"check_apparat|find_my_orders|service_info",
    re.IGNORECASE,
)
_FIGURES = re.compile(r"\d+\s*%|\d+\s*лист")


@dataclass
class Failure:
    case: str
    problem: str


@dataclass
class Report:
    failures: list[Failure] = field(default_factory=list)
    escalated: int = 0
    total: int = 0
    transcript: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def _check(report: Report, case: Case, condition: bool, problem: str) -> None:
    if not condition:
        report.failures.append(Failure(case.name, problem))


async def run_case(case: Case, report: Report) -> None:
    api = FakeApi(case.world)
    conversation = await memory.current_conversation("884013433", "zhan")
    await storage.close_conversation(conversation.id)
    conversation = await memory.current_conversation("884013433", "zhan")

    result = None
    tools: list[str] = []
    reached_staff = False
    report.transcript.append(f"\n=== {case.name}")
    for text in case.turns:
        await memory.remember_user_message(conversation.id, text)
        ctx = TurnContext("884013433", "zhan", conversation.id, text, api=api)
        result = await run_turn(ctx)
        tools += result.tool_calls
        reached_staff = reached_staff or result.needs_staff
        report.transcript.append(f"👤 {text}")
        report.transcript.append(f"🤖 [{result.kind}] {result.text}")
        report.transcript.append(f"   🔧 {result.tool_calls}  🔘 {result.buttons or '—'}")

        # Invariants that hold on every single turn, not just the last one.
        _check(report, case, bool(result.text), "ход закончился без ответа юзеру")
        said = result.text or ""
        leak = _INTERNALS.search(said)
        _check(report, case, leak is None, f"внутренний термин в ответе: «{leak.group(0) if leak else ''}»")
        figure = _FIGURES.search(said)
        _check(report, case, figure is None, f"наши цифры в ответе: «{figure.group(0) if figure else ''}»")
        offending = guards.self_service_advice(said)
        _check(report, case, offending is None, f"совет обслуживать аппарат: «{offending}»")
        _check(
            report, case,
            not guards.promises_a_handoff(said) or result.needs_staff,
            "обещал передать сотруднику, но заявка не создана",
        )
        _check(report, case, not case.world.refund_attempts, "агент попытался вернуть деньги")

    report.total += 1
    if reached_staff:
        report.escalated += 1

    lowered = (result.text or "").lower()
    if case.escalates is not None:
        # Anywhere in the case: escalating on the first message and then
        # answering a follow-up is the right shape, not a miss.
        _check(report, case, reached_staff == case.escalates,
               f"эскалация={reached_staff}, ожидалась {case.escalates}")
    for tool in case.uses:
        _check(report, case, tool in tools, f"не вызвал {tool}")
    for tool in case.avoids:
        _check(report, case, tool not in tools, f"зря вызвал {tool}")
    for phrase in case.says:
        _check(report, case, phrase.lower() in lowered, f"не сказал «{phrase}»")
    for phrase in case.never_says:
        _check(report, case, phrase.lower() not in lowered, f"сказал «{phrase}»")
    if case.asks:
        _check(report, case, bool(result.buttons), "не предложил вариантов кнопками")


async def run_all(cases=None) -> Report:
    storage.settings.support_bot_db_path = tempfile.mktemp(suffix=".sqlite3")
    storage.init_db()
    report = Report()
    for case in cases or CASES:
        # Each case owns its clock: "ночью нечего смотреть" is a different
        # scenario from the same words at two in the afternoon.
        moment = datetime(2026, 9, 9, case.hour, 0)
        for module in (tz, apparats, diagnosis, reading, storage):
            if hasattr(module, "tz"):
                module.tz.now = lambda m=moment: m
        tz.now = lambda m=moment: m
        try:
            await run_case(case, report)
        except Exception as exc:  # a scenario that explodes is a failure, not a crash
            report.failures.append(Failure(case.name, f"упал: {type(exc).__name__}: {exc}"))
    return report


def main() -> int:
    report = asyncio.run(run_all())
    print("\n".join(report.transcript))
    print("\n" + "=" * 74)
    rate = report.escalated / report.total if report.total else 0
    print(f"сценариев: {report.total}   эскалаций: {report.escalated} ({rate:.0%})")
    if report.ok:
        print("✅ всё сошлось")
        return 0
    print(f"❌ расхождений: {len(report.failures)}")
    for f in report.failures:
        print(f"  • [{f.case}] {f.problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
