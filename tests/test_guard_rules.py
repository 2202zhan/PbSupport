import guard_rules
import tz
from api_client import Apparat, Transaction
from diagnosis import Evidence, TicketInput
from guard_rules import Decision, apply_guards


def _ticket(**overrides) -> TicketInput:
    defaults = dict(
        problem_type="not_printed",
        apparat_name_text="Аппарат №3",
        telegram_id="123",
        username="user",
        contact=None,
        raw_text="не печатает",
        submitted_at=tz.now(),
    )
    defaults.update(overrides)
    return TicketInput(**defaults)


def _transaction(amount: float = 100, tx_id: str = "tx-1") -> Transaction:
    return Transaction(
        id=tx_id,
        date=tz.now(),
        machine="Аппарат №3",
        user="@user",
        telegram_id="123",
        amount=amount,
        status="paid",
        payment_method="kaspi",
        print_type="bw",
    )


def _apparat() -> Apparat:
    return Apparat(id=3, name_apparat="Аппарат №3", address="Главный корпус", status="online")


def _evidence(**overrides) -> Evidence:
    defaults = dict(
        ticket=_ticket(),
        identity_confirmed=True,
        transaction=_transaction(),
        apparat=_apparat(),
        mass_outage_suspected=False,
        already_refunded=False,
    )
    defaults.update(overrides)
    return Evidence(**defaults)


def _refund_decision(reason: str = "clear technical failure") -> Decision:
    return Decision(action="auto_refund", reason=reason)


def test_auto_refund_disabled_by_default_forces_escalation():
    # AUTO_REFUND_ENABLED=false is the current testing-phase default (human always
    # confirms via the button) - this should hold without touching settings.
    decision, guard = apply_guards(_evidence(), _refund_decision())
    assert decision.action == "escalate"
    assert guard == "auto_refund_disabled"


def test_clean_case_passes_through_once_auto_refund_enabled(monkeypatch):
    monkeypatch.setattr(guard_rules.settings, "auto_refund_enabled", True)
    decision, guard = apply_guards(_evidence(), _refund_decision())
    assert decision.action == "auto_refund"
    assert guard is None


def test_unconfirmed_identity_forces_escalation(monkeypatch):
    monkeypatch.setattr(guard_rules.settings, "auto_refund_enabled", True)
    evidence = _evidence(identity_confirmed=False)
    decision, guard = apply_guards(evidence, _refund_decision())
    assert decision.action == "escalate"
    assert guard == "identity_unconfirmed"


def test_transaction_ambiguous_forces_escalation(monkeypatch):
    # Several of the user's own orders matched the time/amount window and we
    # couldn't tell them apart (see diagnosis._find_transaction) - the chosen
    # transaction is a guess, so auto_refund against it is a real money risk.
    monkeypatch.setattr(guard_rules.settings, "auto_refund_enabled", True)
    evidence = _evidence(transaction_ambiguous=True)
    decision, guard = apply_guards(evidence, _refund_decision())
    assert decision.action == "escalate"
    assert guard == "transaction_ambiguous"


def test_mass_outage_forces_escalation(monkeypatch):
    monkeypatch.setattr(guard_rules.settings, "auto_refund_enabled", True)
    evidence = _evidence(mass_outage_suspected=True)
    decision, guard = apply_guards(evidence, _refund_decision())
    assert decision.action == "escalate"
    assert guard == "mass_outage_suspected"


def test_amount_above_hard_cap_forces_escalation(monkeypatch):
    monkeypatch.setattr(guard_rules.settings, "auto_refund_enabled", True)
    evidence = _evidence(transaction=_transaction(amount=999999))
    decision, guard = apply_guards(evidence, _refund_decision())
    assert decision.action == "escalate"
    assert guard == "amount_above_hard_cap"


def test_already_refunded_forces_escalation(monkeypatch):
    monkeypatch.setattr(guard_rules.settings, "auto_refund_enabled", True)
    evidence = _evidence(already_refunded=True)
    decision, guard = apply_guards(evidence, _refund_decision())
    assert decision.action == "escalate"
    assert guard == "already_refunded"


def test_no_transaction_forces_escalation(monkeypatch):
    monkeypatch.setattr(guard_rules.settings, "auto_refund_enabled", True)
    evidence = _evidence(transaction=None)
    decision, guard = apply_guards(evidence, _refund_decision())
    assert decision.action == "escalate"
    assert guard == "no_transaction_matched"


def test_guards_never_touch_give_advice():
    evidence = _evidence(identity_confirmed=False, mass_outage_suspected=True)
    decision, guard = apply_guards(evidence, Decision(action="give_advice", reason="x", user_message="hi"))
    assert decision.action == "give_advice"
    assert guard is None


def test_guards_never_touch_escalate():
    evidence = _evidence()
    original = Decision(action="escalate", reason="ambiguous", staff_summary="please check")
    decision, guard = apply_guards(evidence, original)
    assert decision is original
    assert guard is None
