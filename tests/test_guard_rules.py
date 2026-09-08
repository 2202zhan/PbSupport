import tz
from api_client import Apparat, Transaction
from diagnosis import Evidence, TicketInput
from guard_rules import Decision, review_refund_case


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


def test_clean_case_lets_staff_refund():
    review = review_refund_case(_evidence())
    assert review.can_refund is True
    assert review.blockers == []
    assert review.warnings == []


def test_no_transaction_blocks_the_refund_button():
    # There is nothing to call the refund API with - offering the button
    # would just fail on tap.
    review = review_refund_case(_evidence(transaction=None))
    assert review.can_refund is False
    assert "no_transaction_matched" in review.blockers


def test_already_refunded_blocks_the_refund_button():
    review = review_refund_case(_evidence(already_refunded=True))
    assert review.can_refund is False
    assert "already_refunded" in review.blockers


def test_unconfirmed_identity_blocks_the_refund_button():
    review = review_refund_case(_evidence(identity_confirmed=False))
    assert review.can_refund is False
    assert "identity_unconfirmed" in review.blockers


def test_transaction_ambiguous_blocks_the_refund_button():
    # Several of the user's own orders matched the time/amount window and we
    # couldn't tell them apart (see diagnosis._find_transaction) - refunding
    # the guessed one could return the wrong order.
    review = review_refund_case(_evidence(transaction_ambiguous=True))
    assert review.can_refund is False
    assert "transaction_ambiguous" in review.blockers


def test_mass_outage_warns_but_leaves_the_decision_to_staff():
    review = review_refund_case(_evidence(mass_outage_suspected=True))
    assert review.can_refund is True
    assert "mass_outage_suspected" in review.warnings


def test_large_amount_warns_but_leaves_the_decision_to_staff():
    review = review_refund_case(_evidence(transaction=_transaction(amount=999999)))
    assert review.can_refund is True
    assert "amount_above_cap" in review.warnings


def test_blockers_render_as_human_readable_labels():
    review = review_refund_case(_evidence(transaction=None, identity_confirmed=False))
    labels = review.blocker_labels
    assert len(labels) == 2
    assert all(label != review.blockers[i] for i, label in enumerate(labels))


def test_decision_carries_confidence_and_draft_reply():
    # The AI's strongest verdict is a recommendation with a prepared message -
    # notify.py sends that draft only after a human confirms.
    decision = Decision(
        action="recommend_refund",
        reason="SNMP не зафиксировал печать",
        confidence="high",
        draft_reply="Проверил — принтер не получил файл.",
    )
    assert decision.confidence == "high"
    assert decision.draft_reply
