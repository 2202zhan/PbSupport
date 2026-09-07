"""Deterministic review of a refund case before a human sees it.

The AI cannot refund anything - the strongest verdict it can reach is
`recommend_refund`, and every actual refund is a staff member tapping a button.
So these rules no longer downgrade decisions; they answer a narrower question:
*may staff press "Вернуть" on this card at all, and what should they notice
before they do?*

Blockers are cases where pressing the button would be wrong or would simply
fail - no transaction to refund, a refund already made, money that may not be
this user's, or the wrong one of several orders. Warnings are things a human
should weigh but is perfectly capable of deciding on.
"""

from dataclasses import dataclass, field

from config import settings
from diagnosis import Evidence


@dataclass
class Decision:
    action: str  # "recommend_refund" | "give_advice" | "ask_clarifying_question" | "escalate"
    reason: str
    user_message: str | None = None
    staff_summary: str | None = None
    # Set only for "recommend_refund": how strongly the technical evidence backs
    # the case, and the message to send the user once staff approve it.
    confidence: str | None = None
    draft_reply: str | None = None


_BLOCKER_LABELS = {
    "no_transaction_matched": "не нашли транзакцию — возвращать технически нечего",
    "already_refunded": "по этой транзакции возврат уже был",
    "identity_unconfirmed": "личность юзера не подтверждена по telegram_id",
    "transaction_ambiguous": "у юзера несколько заказов под это время/сумму — можно вернуть не тот",
}

_WARNING_LABELS = {
    "mass_outage_suspected": "похоже на массовый сбой — решайте по всем пострадавшим сразу",
    "amount_above_cap": "сумма выше обычной — стоит взглянуть внимательнее",
}


@dataclass
class RefundReview:
    can_refund: bool
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def blocker_labels(self) -> list[str]:
        return [_BLOCKER_LABELS.get(b, b) for b in self.blockers]

    @property
    def warning_labels(self) -> list[str]:
        return [_WARNING_LABELS.get(w, w) for w in self.warnings]


def review_refund_case(evidence: Evidence) -> RefundReview:
    """Purely a property of the evidence, not of what the AI concluded - staff
    get the same verdict on the same facts regardless of the model's opinion."""
    blockers: list[str] = []
    warnings: list[str] = []

    if evidence.transaction is None:
        blockers.append("no_transaction_matched")
    if evidence.already_refunded:
        blockers.append("already_refunded")
    if not evidence.identity_confirmed:
        blockers.append("identity_unconfirmed")
    if evidence.transaction_ambiguous:
        blockers.append("transaction_ambiguous")

    if evidence.mass_outage_suspected:
        warnings.append("mass_outage_suspected")
    amount = evidence.transaction.amount if evidence.transaction else None
    if amount is not None and amount > settings.refund_review_amount_cap:
        warnings.append("amount_above_cap")

    return RefundReview(can_refund=not blockers, blockers=blockers, warnings=warnings)
