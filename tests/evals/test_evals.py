"""The gate on switching users over to the agent.

Not run by default: it drives a real model, so it is slow, costs money and is
not deterministic. `pytest -m evals` when you want to know whether the agent is
good enough yet.
"""
import pytest

from tests.evals.run import run_all


@pytest.mark.evals
async def test_the_agent_holds_up_across_every_category():
    report = await run_all()
    assert report.ok, "\n" + "\n".join(f"[{f.case}] {f.problem}" for f in report.failures)


@pytest.mark.evals
async def test_most_conversations_do_not_need_a_human():
    # The whole point of the rewrite. A bot that escalates everything is the
    # menu bot with extra steps.
    report = await run_all()
    assert report.escalated / report.total < 0.5, f"{report.escalated}/{report.total}"
