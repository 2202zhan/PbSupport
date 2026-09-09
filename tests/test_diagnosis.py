from datetime import datetime, timedelta

import tz

from api_client import Apparat, PrintBoxAPIError, PrinterStatusEvent, Transaction
import diagnosis
from diagnosis import TicketInput, gather_evidence

APPARAT_ID = 3
APPARAT_NAME = "Аппарат №3"

SUCCESS_LOG = (
    "{ts} - INFO - Reading parameters from stdin"
    "{ts} - INFO - Print process completed successfully"
)
ERROR_LOG = (
    "{ts} - INFO - Reading parameters from stdin"
    "{ts2} - ERROR - Unexpected error during download: timeout"
)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S,000")


class FakeAPI:
    """Hand-written stand-in for PrintBoxAPIClient driven by simple in-memory data,
    so diagnosis.py's pagination/windowing logic runs against realistic shapes."""

    def __init__(self):
        self.transactions: list[Transaction] = []
        self.apparats: list[Apparat] = [Apparat(id=APPARAT_ID, name_apparat=APPARAT_NAME, address="x", status="online")]
        self.printer_history: list[PrinterStatusEvent] = []  # any order, sorted internally
        self.device_logs: dict[int, str] = {}
        self.printer_statuses: list[dict] = [{"apparat_id": APPARAT_ID, "is_online": True}]
        self.telegram_documents: dict[str, list[dict]] = {}
        self.printer_summary: dict = {"offline": 0, "with_errors": 0}
        self.printer_alerts: list[dict] = []
        self.raise_on_logs = False
        self.raise_on_request_logs = False
        self.raise_on_documents = False
        self.request_device_logs_calls = 0

    async def get_transactions(self, telegram_id=None, transaction_id=None, page=1, per_page=50):
        pool = self.transactions
        if telegram_id is not None:
            pool = [t for t in pool if t.telegram_id == telegram_id]
        start = (page - 1) * per_page
        return pool[start : start + per_page]

    async def get_apparats(self):
        return self.apparats

    async def get_printer_history(self, apparat_id, limit=50, offset=0):
        pool = sorted(
            (e for e in self.printer_history), key=lambda e: e.created_at, reverse=True
        )
        return pool[offset : offset + limit]

    async def get_all_printer_statuses(self):
        return self.printer_statuses

    async def get_printer_summary(self):
        return self.printer_summary

    async def get_printer_alerts(self):
        return self.printer_alerts

    async def request_device_logs(self, apparat_id, lines=200, log_type="print"):
        self.request_device_logs_calls += 1
        if self.raise_on_logs or self.raise_on_request_logs:
            raise PrintBoxAPIError("offline")

    async def get_device_logs(self, apparat_id, log_type="print"):
        if self.raise_on_logs:
            raise PrintBoxAPIError("offline")
        return self.device_logs.get(apparat_id, "")

    async def get_telegram_documents(self, telegram_id):
        if self.raise_on_documents:
            raise PrintBoxAPIError("documents endpoint 500")
        return self.telegram_documents.get(telegram_id, [])


def _ticket(telegram_id="123", problem_type="not_printed", **overrides) -> TicketInput:
    defaults = dict(
        problem_type=problem_type,
        apparat_name_text=APPARAT_NAME,
        telegram_id=telegram_id,
        username="user",
        contact=None,
        raw_text="не вышла распечатка",
        submitted_at=tz.now(),
    )
    defaults.update(overrides)
    return TicketInput(**defaults)


def _tx(telegram_id: str, date: datetime, tx_id: str) -> Transaction:
    return Transaction(
        id=tx_id, date=date, machine=APPARAT_NAME, user=f"@{telegram_id}", telegram_id=telegram_id,
        amount=100, status="paid", payment_method="kaspi", print_type="bw",
    )


async def test_clean_single_failure_no_mass_outage(monkeypatch):
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    base = datetime(2026, 6, 18, 12, 0, 0)

    # 5 neighbors before, target, 5 neighbors after - 90s apart (busy printer),
    # each neighbor's own signal-window is bounded by the *next* order's time,
    # so neighbor Printing events can't leak into the target's window.
    for i in range(-5, 6):
        when = base + timedelta(seconds=90 * i)
        tg_id = "123" if i == 0 else f"other-{i}"
        api.transactions.append(_tx(tg_id, when, f"tx-{i}"))
        if i != 0:
            # neighbors succeed: SNMP saw Printing right after payment.
            api.printer_history.append(
                PrinterStatusEvent(is_online=True, status="Printing", error_text=None,
                                    created_at=when + timedelta(seconds=10))
            )

    # Target (i == 0) has no Printing event -> print signal not confirmed (a
    # confident "no" from SNMP). That alone doesn't explain *why* it failed, so
    # logs should still be checked - and here they show the actual cause.
    api.device_logs[APPARAT_ID] = ERROR_LOG.format(ts=_fmt(base), ts2=_fmt(base + timedelta(seconds=20)))

    evidence = await gather_evidence(api, _ticket(manual_hint_time=base))

    assert evidence.identity_confirmed is True
    assert evidence.print_signal_confirmed is False
    assert evidence.log_download_error is True
    assert evidence.log_print_success is False
    assert evidence.neighbor_failure_count == 0
    assert evidence.mass_outage_suspected is False


async def test_mass_outage_suspected_when_neighbors_also_fail(monkeypatch):
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    base = datetime(2026, 6, 18, 12, 0, 0)

    for i in range(-5, 6):
        when = base + timedelta(seconds=90 * i)
        tg_id = "123" if i == 0 else f"other-{i}"
        api.transactions.append(_tx(tg_id, when, f"tx-{i}"))
        # Only orders far from the target (|i| > 2) get a Printing event - the
        # ones close to it (i in -2..2, excluding 0) fail too, like a mass outage.
        if abs(i) > 2:
            api.printer_history.append(
                PrinterStatusEvent(is_online=True, status="Printing", error_text=None,
                                    created_at=when + timedelta(seconds=10))
            )
    api.device_logs[APPARAT_ID] = ERROR_LOG.format(ts=_fmt(base), ts2=_fmt(base + timedelta(seconds=20)))

    evidence = await gather_evidence(api, _ticket(manual_hint_time=base))

    assert evidence.print_signal_confirmed is False
    assert evidence.neighbor_failure_count >= 2
    assert evidence.mass_outage_suspected is True


async def test_current_health_summary_detects_mass_outage_without_neighbor_scan(monkeypatch):
    # PrintBox's own monitoring already says 2+ apparats are down/erroring
    # right now - that's a faster, more direct mass-outage signal than
    # scanning neighbor transactions one by one, and should skip that scan.
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    base = datetime(2026, 6, 18, 12, 0, 0)
    api.transactions.append(_tx("123", base, "tx-0"))
    api.printer_summary = {"offline": 2, "with_errors": 0}

    evidence = await gather_evidence(api, _ticket(manual_hint_time=base))

    assert evidence.mass_outage_suspected is True
    assert evidence.neighbor_total_checked == 0  # neighbor scan was skipped


async def test_current_health_alert_surfaced_without_forcing_mass_outage(monkeypatch):
    # This specific apparat has an active warning/critical alert right now -
    # surfaced as apparat_active_alert - but that alone doesn't mean *mass*
    # outage (only one apparat affected), so the historical neighbor scan
    # still runs to decide mass_outage_suspected.
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    base = datetime(2026, 6, 18, 12, 0, 0)
    api.transactions.append(_tx("123", base, "tx-0"))
    api.printer_alerts = [
        {"apparat_id": APPARAT_ID, "alert_type": "warning", "alert_level": "warning",
         "message": "Тонер чёрный: 8%", "since": base.isoformat()}
    ]

    evidence = await gather_evidence(api, _ticket(manual_hint_time=base))

    assert evidence.apparat_active_alert == "Тонер чёрный: 8%"
    assert evidence.mass_outage_suspected is False  # only one apparat affected


async def test_current_health_ignores_info_level_alerts(monkeypatch):
    # alert_level "info" (e.g. just a non-standard status like "Other") isn't
    # an actionable problem - only critical/warning should surface.
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    base = datetime(2026, 6, 18, 12, 0, 0)
    api.transactions.append(_tx("123", base, "tx-0"))
    api.printer_alerts = [
        {"apparat_id": APPARAT_ID, "alert_type": "warning", "alert_level": "info",
         "message": "Статус принтера: Other", "since": base.isoformat()}
    ]

    evidence = await gather_evidence(api, _ticket(manual_hint_time=base))

    assert evidence.apparat_active_alert is None


async def test_printer_error_text_propagated_to_evidence(monkeypatch):
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    base = datetime(2026, 6, 18, 12, 0, 0)
    api.transactions.append(_tx("123", base, "tx-0"))
    api.printer_statuses = [{"apparat_id": APPARAT_ID, "is_online": True, "error_text": "Замята бумага"}]

    evidence = await gather_evidence(api, _ticket(manual_hint_time=base))

    assert evidence.printer_error_text == "Замята бумага"


async def test_neighbors_with_unavailable_snmp_not_counted_as_failures(monkeypatch):
    # If SNMP has no history at all for this apparat, every neighbor check comes
    # back "unknown" (None), not "failed" (False) - an outage detector that
    # can't tell the difference between "no data" and "confirmed broken" would
    # raise false mass-outage alarms on a brand-new or rarely-polled apparat.
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    base = datetime(2026, 6, 18, 12, 0, 0)
    for i in range(-5, 6):
        when = base + timedelta(seconds=90 * i)
        tg_id = "123" if i == 0 else f"other-{i}"
        api.transactions.append(_tx(tg_id, when, f"tx-{i}"))
    # printer_history stays empty for everyone, target included.

    evidence = await gather_evidence(api, _ticket(manual_hint_time=base))

    assert evidence.print_signal_confirmed is None
    assert evidence.neighbor_failure_count == 0
    assert evidence.mass_outage_suspected is False


async def test_logs_are_read_even_when_snmp_confirms_the_print(monkeypatch):
    # SNMP only knows the printer entered a Printing state, not whose file it
    # was; the logs are anchored on this document's own name. Ticket #51 was
    # that gap - a confirmed signal, a user holding nothing, and no log
    # evidence gathered to tell which was right.
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    base = datetime(2026, 6, 18, 12, 0, 0)
    api.transactions.append(_tx("123", base, "tx-0"))
    api.printer_history.append(
        PrinterStatusEvent(is_online=True, status="Printing", error_text=None,
                            created_at=base + timedelta(seconds=10))
    )
    api.device_logs[APPARAT_ID] = ERROR_LOG.format(ts=_fmt(base), ts2=_fmt(base + timedelta(seconds=20)))

    evidence = await gather_evidence(api, _ticket(manual_hint_time=base))

    assert evidence.print_signal_confirmed is True
    # The contradiction is preserved rather than hidden - weighing it is the
    # decision layer's job (see ai_decider's rule 3).
    assert evidence.log_download_error is True
    assert evidence.log_print_success is False


async def test_log_check_uses_filename_hint_to_disambiguate_busy_apparat(monkeypatch):
    # Two jobs' log lines land in the same window (busy printer). Without the
    # filename hint (looked up from our own document records), the window would
    # contain a "completed successfully" line belonging to someone *else's* job
    # and look like a false success.
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    base = datetime(2026, 6, 18, 12, 0, 0)
    api.transactions.append(_tx("123", base, "tx-0"))
    api.telegram_documents["123"] = [
        {"created_at": base.isoformat(), "file_name": "my_report.pdf", "status": "completed"}
    ]
    # printer_history stays empty -> SNMP has nothing, falls back to logs.
    api.device_logs[APPARAT_ID] = (
        f"{_fmt(base)} - INFO - Downloaded file: someone_else.pdf to /tmp/someone_else.pdf"
        f"{_fmt(base)} - INFO - Print process completed successfully"
        f"{_fmt(base + timedelta(seconds=5))} - INFO - Downloaded file: my_report.pdf to /tmp/my_report.pdf"
        f"{_fmt(base + timedelta(seconds=6))} - ERROR - Unexpected error during download: timeout"
    )

    evidence = await gather_evidence(api, _ticket(manual_hint_time=base))

    assert evidence.log_download_error is True
    assert evidence.log_print_success is False


async def test_multiple_own_orders_disambiguated_by_amount(monkeypatch):
    # User paid for two documents close together (e.g. sent several files);
    # only one matches the amount they told us, so we shouldn't fall back to
    # guessing by recency - the wrong one could be the one that printed fine.
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    base = datetime(2026, 6, 18, 12, 0, 0)
    tx_a = _tx("123", base, "tx-a")
    tx_a.amount = 35
    tx_b = _tx("123", base + timedelta(seconds=60), "tx-b")
    tx_b.amount = 100
    api.transactions.extend([tx_a, tx_b])

    ticket = _ticket(
        manual_hint_time=base + timedelta(seconds=30),
        manual_hint_time_tolerance_seconds=120,
        manual_hint_amount=100,
        manual_hint_amount_tolerance=5,
    )
    evidence = await gather_evidence(api, ticket)

    assert evidence.transaction.id == "tx-b"
    assert evidence.transaction_ambiguous is False


async def test_multiple_own_orders_disambiguated_by_print_signal(monkeypatch):
    # Amount doesn't narrow it down here (none given), but SNMP does: one of
    # the two orders actually shows no print signal - that's the one
    # consistent with "paid but nothing came out", not whichever is newest.
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    base = datetime(2026, 6, 18, 12, 0, 0)
    tx_a = _tx("123", base, "tx-a")  # printed fine
    tx_b = _tx("123", base + timedelta(minutes=10), "tx-b")  # the real complaint
    api.transactions.extend([tx_a, tx_b])
    api.printer_history.append(
        PrinterStatusEvent(is_online=True, status="Printing", error_text=None,
                            created_at=base + timedelta(seconds=10))
    )  # only tx_a gets a Printing event

    ticket = _ticket(
        manual_hint_time=base + timedelta(minutes=5),
        manual_hint_time_tolerance_seconds=600,  # wide bucket guess, catches both
    )
    evidence = await gather_evidence(api, ticket)

    assert evidence.transaction.id == "tx-b"
    assert evidence.transaction_ambiguous is False


async def test_multiple_own_orders_truly_ambiguous_falls_back_to_most_recent(monkeypatch):
    # Neither amount nor print signal narrows it down (no SNMP data for
    # either) - picking the most recent is the best available guess, but it
    # must be flagged as a guess, not treated as a confirmed match.
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    base = datetime(2026, 6, 18, 12, 0, 0)
    tx_a = _tx("123", base, "tx-a")
    tx_b = _tx("123", base + timedelta(minutes=10), "tx-b")
    api.transactions.extend([tx_a, tx_b])
    # printer_history stays empty -> SNMP has nothing to say for either.

    ticket = _ticket(
        manual_hint_time=base + timedelta(minutes=5),
        manual_hint_time_tolerance_seconds=600,
    )
    evidence = await gather_evidence(api, ticket)

    assert evidence.transaction.id == "tx-b"
    assert evidence.transaction_ambiguous is True


async def test_identity_unconfirmed_when_no_transaction_for_telegram_id(monkeypatch):
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    base = datetime(2026, 6, 18, 12, 0, 0)
    # Someone else's account placed the order; the ticket author's id has none.
    api.transactions.append(_tx("999-real-payer", base, "tx-0"))

    # Precise (receipt-derived) hint - this is the one case the cross-user
    # fallback search is meant for.
    ticket = _ticket(
        telegram_id="123", manual_hint_time=base, manual_hint_amount=100, manual_hint_is_precise=True
    )
    evidence = await gather_evidence(api, ticket)

    assert evidence.identity_confirmed is False
    assert evidence.transaction is not None
    assert evidence.transaction.id == "tx-0"


async def test_manual_hint_tolerance_widens_fallback_match(monkeypatch):
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    base = datetime(2026, 6, 18, 12, 0, 0)
    real_tx = _tx("999-real-payer", base, "tx-0")
    real_tx.amount = 120  # a bit off from the user's rough "~100" guess
    api.transactions.append(real_tx)

    # Exact tolerance (default) - too narrow, the rough guess shouldn't match.
    strict = _ticket(
        telegram_id="123",
        manual_hint_time=base,
        manual_hint_amount=100,
        manual_hint_is_precise=True,
    )
    evidence = await gather_evidence(api, strict)
    assert evidence.transaction is None

    # Wider tolerance, but still marked precise (e.g. a slightly blurry receipt
    # scan) - same data should now match.
    lenient = _ticket(
        telegram_id="123",
        manual_hint_time=base,
        manual_hint_amount=100,
        manual_hint_amount_tolerance=75,
        manual_hint_is_precise=True,
    )
    evidence = await gather_evidence(api, lenient)
    assert evidence.transaction is not None
    assert evidence.transaction.id == "tx-0"
    assert evidence.identity_confirmed is False


async def test_rough_bucket_hint_never_triggers_cross_user_fallback(monkeypatch):
    # This is the bug from real testing: a user picks a rough "when"/"amount"
    # bucket (no receipt), and on a busy apparat that's wide enough to
    # coincidentally match some *other* real user's unrelated order. Without
    # manual_hint_is_precise, the fallback must not run at all - "nothing
    # found" is the honest answer here, not a wrong, confident-looking match.
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    base = datetime(2026, 6, 18, 12, 0, 0)
    other_users_tx = _tx("999-someone-else", base, "tx-0")
    other_users_tx.amount = 120  # well within a loose "50-150 ₸" bucket's tolerance
    api.transactions.append(other_users_tx)

    ticket = _ticket(
        telegram_id="123",
        manual_hint_time=base,
        manual_hint_amount=100,
        manual_hint_amount_tolerance=75,  # the loose tolerance a bucket guess sets
        manual_hint_is_precise=False,  # default - no receipt was given
    )
    evidence = await gather_evidence(api, ticket)

    assert evidence.transaction is None
    assert evidence.identity_confirmed is False


async def test_old_unrelated_order_not_matched_as_recent(monkeypatch):
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    # This telegram_id's only order on file is from the day before - it
    # shouldn't be mistaken for "the" order behind a complaint about something
    # that just happened.
    old_order_time = datetime(2026, 6, 17, 9, 0, 0)
    api.transactions.append(_tx("123", old_order_time, "tx-old"))

    recent_complaint_time = datetime(2026, 6, 18, 12, 0, 0)
    evidence = await gather_evidence(api, _ticket(manual_hint_time=recent_complaint_time))

    assert evidence.transaction is None
    assert evidence.identity_confirmed is False


async def test_has_receipt_photo_reflected_in_evidence_dict():
    api = FakeAPI()
    ticket = _ticket(receipt_photo_file_id="file-123")
    evidence = await gather_evidence(api, ticket)
    assert evidence.to_dict()["has_receipt_photo"] is True


async def test_device_logs_unavailable_is_handled_gracefully(monkeypatch):
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    api.raise_on_logs = True
    base = datetime(2026, 6, 18, 12, 0, 0)
    api.transactions.append(_tx("123", base, "tx-0"))

    evidence = await gather_evidence(api, _ticket(manual_hint_time=base))

    assert evidence.log_download_error is None
    assert evidence.log_print_success is None


async def test_saved_logs_used_as_snmp_fallback_without_live_request(monkeypatch):
    # SNMP has nothing for this apparat at all (no history) - only then do we
    # fall back to logs, and even then the device already pushed these logs
    # earlier (e.g. right after printing), so checking a report from hours
    # later with the PC now offline overnight should still work from the saved
    # copy without needing the live (failing) "request fresh logs" round trip.
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    api.raise_on_request_logs = True  # a live "push fresh logs" request would fail right now
    base = datetime(2026, 6, 18, 12, 0, 0)
    api.transactions.append(_tx("123", base, "tx-0"))
    # printer_history stays empty -> SNMP has nothing to say for this apparat.
    api.device_logs[APPARAT_ID] = SUCCESS_LOG.format(ts=_fmt(base))

    evidence = await gather_evidence(api, _ticket(manual_hint_time=base))

    assert evidence.print_signal_confirmed is None
    assert evidence.log_print_success is True
    assert evidence.log_download_error is False
    assert api.request_device_logs_calls == 0


async def test_payment_error_also_runs_technical_checks(monkeypatch):
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    base = datetime(2026, 6, 18, 12, 0, 0)
    api.transactions.append(_tx("123", base, "tx-0"))
    # No SNMP history at all for this apparat -> falls back to logs, same
    # technical-signal pipeline as "not_printed" should run for this category too.
    api.device_logs[APPARAT_ID] = ERROR_LOG.format(ts=_fmt(base), ts2=_fmt(base + timedelta(seconds=20)))

    evidence = await gather_evidence(api, _ticket(problem_type="payment_error", manual_hint_time=base))

    assert evidence.print_signal_confirmed is None
    assert evidence.log_download_error is True


async def test_print_quality_no_longer_runs_technical_checks(monkeypatch):
    # print_quality is now a fully-scripted UI path in triage.py (no receipt/time
    # questions, no SNMP/log lookups) - gather_evidence should leave the
    # technical-signal fields untouched for it, even with a matching transaction.
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    base = datetime(2026, 6, 18, 12, 0, 0)
    api.transactions.append(_tx("123", base, "tx-0"))
    api.printer_history.append(
        PrinterStatusEvent(is_online=True, status="Printing", error_text=None,
                            created_at=base + timedelta(seconds=10))
    )
    api.device_logs[APPARAT_ID] = SUCCESS_LOG.format(ts=_fmt(base))

    evidence = await gather_evidence(api, _ticket(problem_type="print_quality", manual_hint_time=base))

    assert evidence.print_signal_confirmed is None
    assert evidence.log_print_success is None


async def test_toner_levels_captured_in_evidence(monkeypatch):
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    api.printer_statuses = [
        {"apparat_id": APPARAT_ID, "is_online": True, "toner": {"black": 8, "cyan": 90}}
    ]
    base = tz.now()
    api.transactions.append(_tx("123", base, "tx-0"))

    evidence = await gather_evidence(api, _ticket(problem_type="print_quality"))

    assert evidence.toner_levels == {"black": 8, "cyan": 90}


async def test_upload_failed_no_document_found():
    api = FakeAPI()
    evidence = await gather_evidence(api, _ticket(problem_type="upload_failed"))
    assert evidence.document_found is False
    assert evidence.document_status is None


async def test_upload_failed_document_check_failed_does_not_crash():
    # documents API 500s - must come back as "couldn't check" (None), and
    # must not blow up the rest of gather_evidence.
    api = FakeAPI()
    api.raise_on_documents = True
    evidence = await gather_evidence(api, _ticket(problem_type="upload_failed"))
    assert evidence.document_found is None
    assert evidence.document_status is None


async def test_upload_failed_document_found_with_status():
    api = FakeAPI()
    now = tz.now()
    api.telegram_documents["123"] = [
        {"created_at": now.isoformat(), "status": "error"},
    ]
    evidence = await gather_evidence(api, _ticket(problem_type="upload_failed"))
    assert evidence.document_found is True
    assert evidence.document_status == "error"


async def test_has_recent_document_true_within_window():
    api = FakeAPI()
    now = tz.now()
    api.telegram_documents["123"] = [{"created_at": now.isoformat(), "status": "completed"}]
    assert await diagnosis.has_recent_document(api, "123", now, hours=24) is True


async def test_has_recent_document_false_when_too_old():
    api = FakeAPI()
    now = tz.now()
    old = now - timedelta(hours=30)
    api.telegram_documents["123"] = [{"created_at": old.isoformat(), "status": "completed"}]
    assert await diagnosis.has_recent_document(api, "123", now, hours=24) is False


async def test_has_recent_document_false_when_none_at_all():
    api = FakeAPI()
    assert await diagnosis.has_recent_document(api, "123", tz.now(), hours=24) is False


async def test_has_recent_document_none_when_check_fails():
    # The real bug this guards against: the documents API occasionally 500s.
    # That must come back as "couldn't check" (None), never silently as
    # "confirmed no file" (False) - those lead to very different messages.
    api = FakeAPI()
    api.raise_on_documents = True
    assert await diagnosis.has_recent_document(api, "123", tz.now(), hours=24) is None


async def test_not_printed_also_populates_document_found(monkeypatch):
    # documents are checked for the technical categories too now, not just
    # upload_failed - this is the "did this account upload anything at all"
    # signal that runs before transactions/SNMP.
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    base = datetime(2026, 6, 18, 12, 0, 0)
    api.transactions.append(_tx("123", base, "tx-0"))
    api.telegram_documents["123"] = [{"created_at": base.isoformat(), "status": "completed"}]

    evidence = await gather_evidence(api, _ticket(manual_hint_time=base))

    assert evidence.document_found is True


async def test_not_printed_document_found_false_when_no_documents_at_all(monkeypatch):
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    base = datetime(2026, 6, 18, 12, 0, 0)
    api.transactions.append(_tx("123", base, "tx-0"))
    # api.telegram_documents stays empty.

    evidence = await gather_evidence(api, _ticket(manual_hint_time=base))

    assert evidence.document_found is False


async def _no_sleep(*args, **kwargs):
    return None


async def test_simultaneous_orders_are_not_counted_as_failures(monkeypatch):
    # Real pattern from Аппарат №3: people send several documents at once and
    # the kiosk writes them as separate transactions in the same second. Each
    # one bounded by the next left a window that closed before the order was
    # even placed, so no print could fall inside it - and a busy minute was
    # reported as "7 of 10 neighbours without signal".
    monkeypatch.setattr(diagnosis.asyncio, "sleep", _no_sleep)
    api = FakeAPI()
    base = datetime(2026, 9, 8, 14, 13, 43)

    # Three bursts of three simultaneous orders, all printing fine.
    for burst in range(3):
        when = base + timedelta(minutes=15 * burst)
        for n in range(3):
            api.transactions.append(_tx("other", when, f"tx-{burst}-{n}"))
        api.printer_history.append(
            PrinterStatusEvent(is_online=True, status="Printing", error_text=None,
                                created_at=when + timedelta(seconds=20))
        )
    target_time = base + timedelta(minutes=4)
    api.transactions.append(_tx("123", target_time, "tx-target"))
    api.printer_history.append(
        PrinterStatusEvent(is_online=True, status="Printing", error_text=None,
                            created_at=target_time + timedelta(seconds=15))
    )

    evidence = await gather_evidence(api, _ticket(manual_hint_time=target_time))

    assert evidence.mass_outage_suspected is False
    assert evidence.neighbor_failure_count == 0
    # Only the separable orders are counted at all.
    assert evidence.neighbor_total_checked < 9


def test_signal_bounds_refuses_a_window_that_closes_before_the_order():
    base = datetime(2026, 9, 8, 14, 13, 43)
    burst = [_tx("u", base, "a"), _tx("u", base, "b"), _tx("u", base + timedelta(minutes=4), "c")]
    # First of a same-second pair: nothing can be attributed to it.
    assert diagnosis._signal_bounds(burst, 0) is None
    # Last before a distant order: a normal, usable window.
    bounds = diagnosis._signal_bounds(burst, 1)
    assert bounds is not None
    lower, upper = bounds
    assert upper > burst[1].date


class _ApparatDirectory:
    def __init__(self, apparats):
        self._apparats = apparats

    async def get_apparats(self):
        return self._apparats


def _named(id, name, address):
    return Apparat(id=id, name_apparat=name, address=address, status="online")


async def test_apparat_is_found_by_the_place_the_user_named():
    # Students name the building, not the machine - "главный корпус" has to
    # land on the kiosk that stands there.
    api = _ApparatDirectory([
        _named(1, "Аппарат №1", "Главный корпус, 1 этаж, Чилл зона"),
        _named(2, "Аппарат №2", "Второй корпус"),
    ])
    found = await diagnosis.find_apparat_by_name(api, "главный корпус")
    assert found is not None and found.id == 1


async def test_an_ambiguous_place_is_not_guessed():
    # "корпус" is in every address - attaching the ticket to one of them at
    # random would send staff to the wrong building.
    api = _ApparatDirectory([
        _named(1, "Аппарат №1", "Главный корпус"),
        _named(2, "Аппарат №2", "Второй корпус"),
    ])
    assert await diagnosis.find_apparat_by_name(api, "корпус") is None


async def test_the_machine_name_still_wins_over_an_address():
    api = _ApparatDirectory([
        _named(1, "Аппарат №1", "Второй корпус"),
        _named(2, "Аппарат №2", "Главный корпус"),
    ])
    found = await diagnosis.find_apparat_by_name(api, "Аппарат №2")
    assert found is not None and found.id == 2


async def test_a_declined_machine_name_still_matches():
    # "бледно печатает на аппарате 3" is how people write it. The plain
    # substring test failed on the case ending and on the display name's own
    # emoji digit, and the bot answered "такого аппарата не нашёл".
    api = _ApparatDirectory([
        _named(3, "Аппарат №3 3️⃣", "Первый корпус"),
        _named(2, "Аппарат №2", "Второй корпус"),
    ])
    for said in ["аппарат 3", "на аппарате 3", "Аппарата №3", "аппарат №3"]:
        found = await diagnosis.find_apparat_by_name(api, said)
        assert found is not None and found.id == 3, said


async def test_a_different_number_is_still_a_different_machine():
    api = _ApparatDirectory([
        _named(3, "Аппарат №3 3️⃣", "Первый корпус"),
        _named(2, "Аппарат №2", "Второй корпус"),
    ])
    found = await diagnosis.find_apparat_by_name(api, "на аппарате 2")
    assert found is not None and found.id == 2
