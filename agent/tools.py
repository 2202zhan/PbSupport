"""What the agent can do.

The reading tools are where the "no internal terms" rule is enforced
structurally rather than by asking the model nicely: they return facts in plain
Russian, and the figures behind them go into ctx.staff_notes, which the model
never sees and the escalation card does.
"""

from typing import Any

from agent import guards
from agent.reading import CHECK_APPARAT, FIND_MY_ORDERS, INVESTIGATE_ORDER, SERVICE_INFO
from agent.registry import ToolError, ToolRegistry, ToolSpec
from agent.types import TurnContext, TurnResult

__all__ = [
    "DEFAULT_TOOLS", "ToolError", "ToolRegistry", "ToolSpec",
    "REPLY", "ESCALATE", "sanitize_buttons", "MAX_BUTTONS", "EXPECTATIONS",
]

MAX_BUTTONS = 4
MAX_BUTTON_LABEL = 40
EXPECTATIONS = ("text", "choice", "file", "none")


def sanitize_buttons(raw: Any) -> list[str]:
    """Buttons are a convenience, so a malformed one is dropped rather than
    failing the turn - the text of the message still stands on its own."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ToolError("buttons должен быть списком коротких подписей")
    labels: list[str] = []
    for item in raw:
        label = item.get("label") if isinstance(item, dict) else item
        if not isinstance(label, str):
            continue
        label = " ".join(label.split())[:MAX_BUTTON_LABEL]
        if label and label not in labels:
            labels.append(label)
    return labels[:MAX_BUTTONS]


async def _reply(args: dict[str, Any], _ctx: TurnContext) -> TurnResult:
    text = (args.get("text") or "").strip()
    if not text:
        raise ToolError("reply нужен непустой text")
    offending = guards.self_service_advice(text)
    if offending:
        # Recoverable, and worth recovering: the answer may be right apart from
        # this one sentence, so ask for a rewrite rather than losing the turn.
        raise ToolError(
            f"Нельзя советовать юзеру обслуживать аппарат («{offending}») — киоск наш и "
            "закрытый. Перепиши ответ: что можем сделать мы и что реально может он "
            "(распечатать заново, сходить к другому аппарату, прислать чек)."
        )
    buttons = sanitize_buttons(args.get("buttons"))
    expect = args.get("expect")
    if expect not in EXPECTATIONS:
        expect = "choice" if buttons else "text"
    if guards.promises_a_handoff(text):
        # The bot may only claim a hand-off when one happens. Rather than
        # arguing with the model about wording, make what it said true.
        return TurnResult(
            kind="escalate",
            text=text,
            reason="reply_promised_staff",
            staff_summary=f"Агент пообещал юзеру передать обращение сотруднику: «{text}»",
        )
    return TurnResult(kind="reply", text=text, buttons=buttons, expect=expect)


async def _escalate(args: dict[str, Any], _ctx: TurnContext) -> TurnResult:
    summary = (args.get("summary_for_staff") or "").strip()
    if not summary:
        raise ToolError("escalate нужен summary_for_staff")
    draft = (args.get("draft_reply") or "").strip() or None
    if draft and guards.self_service_advice(draft):
        raise ToolError("в draft_reply нельзя советовать юзеру обслуживать аппарат, перепиши")
    confidence = args.get("confidence")
    return TurnResult(
        kind="escalate",
        text=(args.get("message_for_user") or "").strip() or None,
        staff_summary=summary,
        reason=(args.get("reason") or "").strip() or "escalated_by_agent",
        refund_recommended=bool(args.get("refund_recommended")),
        confidence=confidence if confidence in ("high", "medium", "low") else None,
        draft_reply=draft,
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
            },
            "buttons": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "До 4 коротких вариантов ответа под этот конкретный вопрос — то, что "
                    "юзеру иначе пришлось бы печатать. Не список тем и не меню: варианты "
                    "должны отвечать именно на твой вопрос. Если ответ свободный "
                    "(описать проблему, назвать сумму) — не давай кнопок вовсе."
                ),
            },
            "expect": {
                "type": "string",
                "enum": list(EXPECTATIONS),
                "description": (
                    "Что ты ждёшь дальше: 'choice' — выбор из кнопок, 'text' — свободный "
                    "ответ, 'file' — фото или PDF (например чек), 'none' — разговор закончен."
                ),
            },
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
        "нерешённая техническая проблема, агрессия, прямая просьба позвать оператора. "
        "Возврат ты не делаешь никогда: можешь только рекомендовать его сотруднику."
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
            "refund_recommended": {
                "type": "boolean",
                "description": (
                    "Ставь true, если по результатам investigate_order похоже на нашу "
                    "техническую ошибку и деньги стоит вернуть. Это рекомендация "
                    "сотруднику, а не возврат: решает и нажимает кнопку он."
                ),
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "Насколько ты уверен в рекомендации возврата.",
            },
            "draft_reply": {
                "type": "string",
                "description": (
                    "Готовый текст юзеру, который сотрудник отправит, если подтвердит "
                    "возврат. На языке юзера, без обещаний «уже вернули»."
                ),
            },
        },
        "required": ["reason", "summary_for_staff"],
    },
    terminal=True,
    run=_escalate,
)

DEFAULT_TOOLS = ToolRegistry(
    [SERVICE_INFO, CHECK_APPARAT, FIND_MY_ORDERS, INVESTIGATE_ORDER, REPLY, ESCALATE]
)
