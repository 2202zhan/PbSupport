"""What the agent can do.

Two kinds. A data tool answers the model and the loop continues. A terminal
tool ends the turn: it returns a TurnResult and nothing further is asked of the
model. Phase 2 has only the two terminal ones - talking to the user, and
handing the case to a human. The data tools arrive in phase 4.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import ai_decider
from agent.types import TurnContext, TurnResult


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    terminal: bool
    run: Callable[[dict[str, Any], TurnContext], Awaitable[Any]]

    @property
    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolError(Exception):
    """The model called a tool wrongly. Reported back to it as a tool result so
    it can correct itself inside the turn's budget, rather than failing the
    whole turn over a missing argument."""


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec]) -> None:
        self._specs = {s.name: s for s in specs}

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [s.schema for s in self._specs.values()]

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)


async def _reply(args: dict[str, Any], _ctx: TurnContext) -> TurnResult:
    text = (args.get("text") or "").strip()
    if not text:
        raise ToolError("reply нужен непустой text")
    if ai_decider.promises_a_handoff(text):
        # The bot may only claim a hand-off when one happens. Rather than
        # arguing with the model about wording, make what it said true.
        return TurnResult(
            kind="escalate",
            text=text,
            reason="reply_promised_staff",
            staff_summary=f"Агент пообещал юзеру передать обращение сотруднику: «{text}»",
        )
    return TurnResult(kind="reply", text=text)


async def _escalate(args: dict[str, Any], _ctx: TurnContext) -> TurnResult:
    summary = (args.get("summary_for_staff") or "").strip()
    if not summary:
        raise ToolError("escalate нужен summary_for_staff")
    return TurnResult(
        kind="escalate",
        text=(args.get("message_for_user") or "").strip() or None,
        staff_summary=summary,
        reason=(args.get("reason") or "").strip() or "escalated_by_agent",
    )


REPLY = ToolSpec(
    name="reply",
    description=(
        "Ответить юзеру и закончить ход. Никого не уведомляет: сотрудники об этом не "
        "узнают, поэтому в тексте нельзя обещать, что ты кому-то передал обращение."
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Сообщение юзеру на его языке (русский или казахский).",
            }
        },
        "required": ["text"],
    },
    terminal=True,
    run=_reply,
)

ESCALATE = ToolSpec(
    name="escalate",
    description=(
        "Передать обращение живому сотруднику: создаётся заявка, сотрудник видит карточку "
        "и может ответить юзеру напрямую. Вызывай, когда нужен человек — вопрос про деньги, "
        "нерешённая техническая проблема, агрессия, прямая просьба позвать оператора."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Короткая причина, по-русски."},
            "summary_for_staff": {
                "type": "string",
                "description": "Что случилось и что уже известно. Для сотрудника, по-русски.",
            },
            "message_for_user": {
                "type": "string",
                "description": "Что сказать юзеру, пока сотрудник не ответил. На его языке.",
            },
        },
        "required": ["reason", "summary_for_staff"],
    },
    terminal=True,
    run=_escalate,
)

DEFAULT_TOOLS = ToolRegistry([REPLY, ESCALATE])
