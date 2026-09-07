"""Deterministic safety limits applied on top of the AI's decision.

These can only downgrade `auto_refund` to `escalate` - they never upgrade a
decision, and they never touch `give_advice`. The AI is free to use judgment
(tone, amount, technical signal) within these bounds, but money-moving actions
always pass through here first.
"""

from dataclasses import dataclass

from config import settings
from diagnosis import Evidence


@dataclass
class Decision:
    action: str  # "auto_refund" | "give_advice" | "escalate"
    reason: str
    user_message: str | None = None
    staff_summary: str | None = None


_GUARD_LABELS = {
    "auto_refund_disabled": "авто-возврат сейчас выключен (тестовый режим) - нужно подтверждение человека",
    "identity_unconfirmed": "личность юзера не подтверждена по telegram_id",
    "transaction_ambiguous": "у юзера несколько своих заказов под это время/сумму - не уверены, какой именно",
    "mass_outage_suspected": "похоже на массовый сбой - решение нужно по всем сразу, не по одному тикету",
    "already_refunded": "по этой транзакции уже был возврат",
    "amount_above_hard_cap": "сумма выше потолка для авто-возврата",
    "no_transaction_matched": "не нашли транзакцию для возврата",
}


def apply_guards(evidence: Evidence, decision: Decision) -> tuple[Decision, str | None]:
    if decision.action != "auto_refund":
        return decision, None

    if not settings.auto_refund_enabled:
        return _escalate(decision, "auto_refund_disabled"), "auto_refund_disabled"

    if not evidence.identity_confirmed:
        return _escalate(decision, "identity_unconfirmed"), "identity_unconfirmed"

    if evidence.transaction_ambiguous:
        return _escalate(decision, "transaction_ambiguous"), "transaction_ambiguous"

    if evidence.mass_outage_suspected:
        return _escalate(decision, "mass_outage_suspected"), "mass_outage_suspected"

    if evidence.already_refunded:
        return _escalate(decision, "already_refunded"), "already_refunded"

    amount = evidence.transaction.amount if evidence.transaction else None
    if amount is not None and amount > settings.auto_refund_hard_cap:
        return _escalate(decision, "amount_above_hard_cap"), "amount_above_hard_cap"

    if evidence.transaction is None:
        return _escalate(decision, "no_transaction_matched"), "no_transaction_matched"

    return decision, None


def _escalate(original: Decision, guard_name: str) -> Decision:
    label = _GUARD_LABELS.get(guard_name, guard_name)
    return Decision(
        action="escalate",
        reason=f"guard:{guard_name}; original AI reason: {original.reason}",
        staff_summary=f"🤖 ИИ предложил возврат (причина: {original.reason}).\n⚠️ {label}.",
    )
