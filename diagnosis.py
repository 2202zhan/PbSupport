import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import tz
from api_client import Apparat, PrintBoxAPIClient, PrintBoxAPIError, Transaction
from config import settings
from log_parser import entries_between, has_download_error, has_successful_print, parse_log_entries

DEFAULT_RECENCY_WINDOW_SECONDS = 3600

logger = logging.getLogger(__name__)


TECHNICAL_PROBLEM_TYPES = {"not_printed", "payment_error"}


@dataclass
class TicketInput:
    problem_type: str  # "not_printed" | "payment_error" | "print_quality" | "upload_failed" | "other"
    apparat_name_text: str
    telegram_id: str
    username: str | None
    contact: str | None
    raw_text: str
    submitted_at: datetime
    dialogue_history: list[str] = field(default_factory=list)
    manual_hint_amount: float | None = None
    manual_hint_amount_tolerance: float = 1.0
    manual_hint_time: datetime | None = None
    manual_hint_time_tolerance_seconds: float = 900
    # True only when the hints above came from a parsed receipt (exact amount
    # and timestamp) - rough self-reported buckets ("~50-150 ₸", "сегодня
    # раньше") are too loose to safely search *other users'* transactions with;
    # they're only precise enough to narrow down this user's own history.
    manual_hint_is_precise: bool = False
    receipt_photo_file_id: str | None = None
    receipt_is_document: bool = False


@dataclass
class Evidence:
    ticket: TicketInput
    identity_confirmed: bool
    transaction: Transaction | None
    apparat: Apparat | None
    print_signal_confirmed: bool | None = None
    log_download_error: bool | None = None
    log_print_success: bool | None = None
    printer_currently_offline: bool | None = None
    toner_levels: dict | None = None
    printer_error_text: str | None = None
    mass_outage_suspected: bool = False
    neighbor_failure_count: int = 0
    neighbor_total_checked: int = 0
    # Message from PrintBox's own current printer-health monitoring for this
    # specific apparat, if it has an active critical/warning alert right now
    # (offline, low toner, or its own error_text) - see _check_current_health.
    # Independent of mass_outage_suspected, which can also come from the
    # cheap multi-apparat check there instead of the historical neighbor scan.
    apparat_active_alert: str | None = None
    document_found: bool | None = None
    document_status: str | None = None
    already_refunded: bool = False
    # True when several of this same account's own orders matched the
    # user-given time/amount window and we couldn't fully tell them apart
    # (see _find_transaction) - the chosen transaction is a best guess, not a
    # confirmed match, so decisions downstream should be more cautious.
    transaction_ambiguous: bool = False

    def to_dict(self) -> dict:
        return {
            "problem_type": self.ticket.problem_type,
            "apparat_name_text": self.ticket.apparat_name_text,
            "telegram_id": self.ticket.telegram_id,
            "identity_confirmed": self.identity_confirmed,
            "transaction_id": self.transaction.id if self.transaction else None,
            "amount": self.transaction.amount if self.transaction else self.ticket.manual_hint_amount,
            "apparat_id": self.apparat.id if self.apparat else None,
            "apparat_name": self.apparat.name_apparat if self.apparat else None,
            "print_signal_confirmed": self.print_signal_confirmed,
            "log_download_error": self.log_download_error,
            "log_print_success": self.log_print_success,
            "printer_currently_offline": self.printer_currently_offline,
            "toner_levels": self.toner_levels,
            "printer_error_text": self.printer_error_text,
            "mass_outage_suspected": self.mass_outage_suspected,
            "neighbor_failure_count": self.neighbor_failure_count,
            "neighbor_total_checked": self.neighbor_total_checked,
            "apparat_active_alert": self.apparat_active_alert,
            "document_found": self.document_found,
            "document_status": self.document_status,
            "already_refunded": self.already_refunded,
            "has_receipt_photo": bool(self.ticket.receipt_photo_file_id),
            "transaction_ambiguous": self.transaction_ambiguous,
        }


def _normalize(text: str) -> str:
    return "".join(ch.lower() for ch in text if ch.isalnum())


def _machine_matches(machine_name: str, apparat_text: str) -> bool:
    a, b = _normalize(machine_name), _normalize(apparat_text)
    if not a or not b:
        return False
    return a in b or b in a


async def _find_transaction(api: PrintBoxAPIClient, ticket: TicketInput) -> tuple[Transaction | None, bool, bool]:
    """Returns (transaction, identity_confirmed, ambiguous).

    Only transactions reasonably close in time to when the user says this
    happened count as "theirs" - an old, unrelated order on file shouldn't be
    mistaken for the one being complained about just because it's the most
    recent thing this telegram_id ever bought.
    """
    reference_time = ticket.manual_hint_time or tz.now()
    window_seconds = (
        ticket.manual_hint_time_tolerance_seconds if ticket.manual_hint_time else DEFAULT_RECENCY_WINDOW_SECONDS
    )

    candidates = await api.get_transactions(telegram_id=ticket.telegram_id, per_page=50)
    recent = [c for c in candidates if abs((c.date - reference_time).total_seconds()) <= window_seconds]
    if recent:
        matching = [c for c in recent if _machine_matches(c.machine, ticket.apparat_name_text)]
        pool = matching or recent

        # Several of the user's own orders can land in the same matching
        # window (they uploaded/paid for a few documents close together) -
        # picking the most recent one blindly risks grabbing an order that's
        # not the one actually being complained about. Narrow down with what
        # the user already told us before resorting to guessing.
        if len(pool) > 1 and ticket.manual_hint_amount is not None:
            by_amount = [
                t for t in pool if abs(t.amount - ticket.manual_hint_amount) <= ticket.manual_hint_amount_tolerance
            ]
            if by_amount:
                pool = by_amount

        if len(pool) > 1 and ticket.problem_type in TECHNICAL_PROBLEM_TYPES:
            disambiguated = await _pick_unprinted_candidate(api, pool)
            if disambiguated is not None:
                return disambiguated, True, False

        pool.sort(key=lambda t: t.date, reverse=True)
        return pool[0], True, len(pool) > 1

    # Fallback: nothing recent enough under this telegram_id - someone else's
    # account may have placed the order. Search a bounded window of recent
    # transactions across all users for a plausible match; identity stays
    # unconfirmed regardless of whether we find something.
    #
    # Only attempt this with precise (receipt-derived) hints. A rough bucket
    # guess ("~150-500 ₸", "сегодня раньше") is wide enough that on a busy
    # apparat it will coincidentally match some *other* real user's order,
    # producing a confident-looking but wrong transaction - worse than finding
    # nothing, since the rest of the pipeline then reasons about the wrong
    # order entirely. Without a receipt, "nothing found" is the honest answer.
    if ticket.manual_hint_is_precise and (ticket.manual_hint_time or ticket.manual_hint_amount):
        for page in range(1, 6):
            batch = await api.get_transactions(page=page, per_page=100)
            if not batch:
                break
            for t in batch:
                amount_ok = (
                    ticket.manual_hint_amount is None
                    or abs(t.amount - ticket.manual_hint_amount) <= ticket.manual_hint_amount_tolerance
                )
                time_ok = (
                    ticket.manual_hint_time is None
                    or abs((t.date - ticket.manual_hint_time).total_seconds())
                    < ticket.manual_hint_time_tolerance_seconds
                )
                machine_ok = _machine_matches(t.machine, ticket.apparat_name_text)
                if amount_ok and time_ok and machine_ok:
                    return t, False, False
    return None, False, False


async def _pick_unprinted_candidate(api: PrintBoxAPIClient, candidates: list[Transaction]) -> Transaction | None:
    """Disambiguates multiple of the user's own orders that all matched the
    time/amount window (e.g. several documents paid for close together) by
    checking each one's own print signal and preferring the one consistent
    with "paid but nothing came out" - i.e. the one that does *not* show a
    confirmed print. Only returns a pick when that's conclusive (exactly one
    candidate lacks a signal); otherwise returns None and lets the caller
    fall back to "most recent" rather than pretending this resolved anything.
    """
    apparat = await find_apparat_by_name(api, candidates[0].machine)
    if apparat is None:
        return None
    order_pool = await _build_order_pool(api, candidates[0].machine)
    unconfirmed = []
    for t in candidates:
        idx = next((i for i, p in enumerate(order_pool) if p.id == t.id), None)
        if idx is None:
            continue
        bounds = _signal_bounds(order_pool, idx)
        if bounds is None:
            # Can't attribute a signal to this one - it tells us nothing either
            # way, so it must not be picked as "the one that didn't print".
            continue
        signal = await _check_print_signal(api, apparat.id, *bounds)
        if signal is not True:
            unconfirmed.append(t)
    return unconfirmed[0] if len(unconfirmed) == 1 else None


async def find_apparat_by_name(api: PrintBoxAPIClient, machine_name: str) -> Apparat | None:
    apparats = await api.get_apparats()
    for a in apparats:
        if _machine_matches(a.name_apparat, machine_name):
            return a
    return None


async def _fetch_history_covering(
    api: PrintBoxAPIClient, apparat_id: int, until: datetime, max_records: int = 400
):
    history = []
    offset = 0
    page_size = 200
    while offset < max_records:
        batch = await api.get_printer_history(apparat_id, limit=page_size, offset=offset)
        if not batch:
            break
        history.extend(batch)
        oldest = batch[-1].created_at
        if oldest <= until:
            break
        offset += page_size
    return history


async def _build_order_pool(api: PrintBoxAPIClient, machine_name: str) -> list[Transaction]:
    """Fetches a bounded pool of transactions on the same apparat, sorted by time.

    Used both to bound each order's signal-check window by its actual neighbors
    (so a busy printer's next job doesn't get mistaken for this job's signal) and
    to run the mass-outage check.
    """
    window = settings.neighbor_window
    pool: list[Transaction] = []
    for page in range(1, 6):
        batch = await api.get_transactions(page=page, per_page=100)
        if not batch:
            break
        pool.extend(b for b in batch if _machine_matches(b.machine, machine_name))
        if len(pool) >= window * 4:
            break
    pool.sort(key=lambda t: t.date)
    return pool


# A print needs at least this long after payment before its "Printing" event
# can plausibly show up in the SNMP history.
_MIN_SIGNAL_WINDOW = timedelta(seconds=10)


def _signal_bounds(pool: list[Transaction], idx: int) -> tuple[datetime, datetime] | None:
    """Time window to look for *this* order's print signal: from shortly before
    payment to either the configured window or the next order on this apparat,
    whichever comes first - so a neighboring job's signal can't leak in.

    Returns None when the orders are too close together to tell apart. People
    routinely send several documents at once and the kiosk writes them as
    separate transactions in the same second; bounding each by the next one
    then leaves a window that closes before the order was even placed, so no
    print could ever fall inside it. Reporting that as "did not print" turned
    a busy minute into a phantom mass outage - the honest answer is that this
    order's signal cannot be attributed, and the device logs (which carry
    filenames) are the way to tell them apart.
    """
    target = pool[idx]
    lower = target.date - timedelta(seconds=30)
    upper = target.date + timedelta(minutes=settings.print_signal_window_minutes)
    if idx + 1 < len(pool):
        upper = min(upper, pool[idx + 1].date - timedelta(seconds=5))
    if upper - target.date < _MIN_SIGNAL_WINDOW:
        return None
    return lower, upper


async def _check_print_signal(
    api: PrintBoxAPIClient, apparat_id: int, lower: datetime, upper: datetime
) -> bool | None:
    """SNMP/printer-status history is the primary signal - it's recorded
    server-side continuously, independent of whether the device is online right
    now. Returns True/False when we got real history to check (a "no Printing
    event" result is itself a confident negative, not a shrug); returns None
    only if the history call failed or came back completely empty, meaning SNMP
    itself has nothing to say here.

    A True result is trusted on its own (the order is closed out - see
    gather_evidence). False or None both mean "SNMP didn't confirm printing",
    and either way the next step is to check device logs too - False alone
    doesn't explain *why* (download error vs something else), which matters for
    the refund/escalation reasoning and for what we tell the user.
    """
    try:
        history = await _fetch_history_covering(api, apparat_id, until=lower)
    except PrintBoxAPIError:
        logger.warning("SNMP history unavailable for apparat %s", apparat_id)
        return None
    if not history:
        return None
    return any(e.status.lower() == "printing" and lower <= e.created_at <= upper for e in history)


async def _check_device_logs(
    api: PrintBoxAPIClient, apparat_id: int, lower: datetime, upper: datetime, filename_hint: str | None = None
):
    """Returns (download_error_found, print_success_found) or (None, None) if logs
    aren't available for the relevant window at all.

    Tries the already-saved logs first - the device pushes these on its own after
    each print attempt, so they cover what we need even if the PC is offline right
    now (e.g. someone reports an issue from hours ago, at 1am, when the kiosk is
    powered down). Only asks the device to push a fresh batch over its live
    WebSocket connection if the saved copy doesn't already cover the window - that
    live request is the one that actually fails/times out when the PC is offline.

    If filename_hint is given (the document's original filename, looked up from
    our own records) and a nearby entry mentions it (typically "Starting print
    process with URL:..." or "Downloaded file: ..."), use that line's timestamp
    as an anchor and narrow to a tight window right after it - on a busy
    apparat, several jobs can land in the same time window, and the filename
    pins down which one is actually this job's. We anchor rather than filter
    every line by filename, because the ERROR/success lines that actually
    matter often don't repeat the filename themselves (e.g. a download timeout
    is logged as a generic connection error with no file name in it).
    """
    nearby = await _fetch_log_entries_near(api, apparat_id, lower, upper)
    if not nearby:
        try:
            await api.request_device_logs(apparat_id)
            await asyncio.sleep(3)
        except PrintBoxAPIError:
            logger.warning("device offline for apparat %s - can't request fresh logs", apparat_id)
            return None, None
        nearby = await _fetch_log_entries_near(api, apparat_id, lower, upper)
        if not nearby:
            return None, None

    if filename_hint:
        anchor_times = [e.timestamp for e in nearby if filename_hint.lower() in e.message.lower()]
        if anchor_times:
            anchor = min(anchor_times)
            narrowed = [
                e for e in nearby
                if anchor - timedelta(seconds=2) <= e.timestamp <= anchor + timedelta(seconds=30)
            ]
            if narrowed:
                nearby = narrowed

    return has_download_error(nearby), has_successful_print(nearby)


async def _fetch_log_entries_near(api: PrintBoxAPIClient, apparat_id: int, lower: datetime, upper: datetime):
    try:
        content = await api.get_device_logs(apparat_id)
    except PrintBoxAPIError:
        return []
    entries = parse_log_entries(content)
    # Both sides are naive Asia/Almaty wall-clock (see tz.py) - directly comparable.
    return entries_between(entries, lower, upper)


async def _find_document_filename(api: PrintBoxAPIClient, telegram_id: str, around: datetime) -> str | None:
    """Looks up the name of the document this user uploaded closest to the
    order time, to help pin down which log lines on a busy apparat belong to
    this specific job (see _check_device_logs' filename_hint)."""
    try:
        docs = await api.get_telegram_documents(telegram_id)
    except PrintBoxAPIError:
        return None
    candidates = [d for d in docs if _recent_enough(d, around, minutes=15)]
    if not candidates:
        return None
    candidates.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return candidates[0].get("file_name")


async def _check_document_status(api: PrintBoxAPIClient, ticket: TicketInput) -> tuple[bool | None, str | None]:
    try:
        docs = await api.get_telegram_documents(ticket.telegram_id)
    except PrintBoxAPIError:
        # The documents API is known to occasionally 500 - don't let that
        # crash the whole evidence gathering, just report "couldn't check".
        logger.warning("could not check documents for telegram_id %s", ticket.telegram_id)
        return None, None
    recent = [d for d in docs if _recent_enough(d, ticket.submitted_at)]
    if not recent:
        return False, None
    recent.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return True, recent[0].get("status")


async def has_recent_document(
    api: PrintBoxAPIClient, telegram_id: str, around: datetime, hours: int = 24
) -> bool | None:
    """Used when telling the user plainly that we found no order from them -
    lets the message also say whether we have any uploaded file from this
    account in the window at all, not just whether a payment exists.

    Returns None (not False!) if the check itself failed - the documents API
    is known to occasionally 500. Silently treating "couldn't check" the same
    as "confirmed no file" would tell the user something we don't actually
    know to be true.
    """
    try:
        docs = await api.get_telegram_documents(telegram_id)
    except PrintBoxAPIError:
        logger.warning("could not check documents for telegram_id %s", telegram_id)
        return None
    return any(_recent_enough(d, around, minutes=hours * 60) for d in docs)


def _recent_enough(doc: dict, around: datetime, minutes: int = 30) -> bool:
    # created_at is naive Asia/Almaty wall-clock, same convention as `around` (see tz.py).
    raw = doc.get("created_at")
    if not raw:
        return False
    try:
        created = datetime.fromisoformat(raw)
    except ValueError:
        return False
    return abs((around - created).total_seconds()) <= minutes * 60


async def gather_evidence(api: PrintBoxAPIClient, ticket: TicketInput) -> Evidence:
    """Checks evidence in a fixed order, cheapest/most-basic first: did this
    account upload anything at all -> is there a payment -> what does SNMP say
    -> only if SNMP itself has nothing to say, check device logs. Each step
    only adds cost once the previous one didn't already give a clear answer."""
    evidence = Evidence(ticket=ticket, identity_confirmed=False, transaction=None, apparat=None)

    if ticket.problem_type == "upload_failed":
        evidence.document_found, evidence.document_status = await _check_document_status(api, ticket)
    elif ticket.problem_type in TECHNICAL_PROBLEM_TYPES:
        # Same "anything from this account at all" signal as upload_failed,
        # just over a wider window anchored on when they say it happened
        # (rather than upload_failed's tight "just now" window).
        reference_time = ticket.manual_hint_time or ticket.submitted_at
        evidence.document_found = await has_recent_document(api, ticket.telegram_id, reference_time)

    transaction, identity_confirmed, transaction_ambiguous = await _find_transaction(api, ticket)
    evidence.transaction = transaction
    evidence.identity_confirmed = identity_confirmed
    evidence.transaction_ambiguous = transaction_ambiguous

    apparat = None
    if transaction is not None:
        apparat = await find_apparat_by_name(api, transaction.machine)
    else:
        apparat = await find_apparat_by_name(api, ticket.apparat_name_text)
    evidence.apparat = apparat

    if apparat is not None:
        statuses = await api.get_all_printer_statuses()
        current = next((s for s in statuses if s.get("apparat_id") == apparat.id), None)
        if current is not None:
            evidence.printer_currently_offline = not current.get("is_online", True)
            evidence.toner_levels = current.get("toner")
            evidence.printer_error_text = current.get("error_text")

    if ticket.problem_type in TECHNICAL_PROBLEM_TYPES and transaction is not None and apparat is not None:
        pool = await _build_order_pool(api, transaction.machine)
        idx = next((i for i, t in enumerate(pool) if t.id == transaction.id), None)
        if idx is None:
            # Target didn't show up in the pool (pagination edge case) - fall back
            # to a plain forward-looking window with no neighbor information.
            pool, idx = [transaction], 0

        bounds = _signal_bounds(pool, idx)
        if bounds is None:
            # Ordered in the same breath as its neighbours - SNMP can't say
            # which of them printed, so widen to the batch and let the device
            # logs, which carry filenames, do the telling apart.
            evidence.print_signal_confirmed = None
            lower = transaction.date - timedelta(seconds=30)
            upper = transaction.date + timedelta(minutes=settings.print_signal_window_minutes)
        else:
            lower, upper = bounds
            evidence.print_signal_confirmed = await _check_print_signal(api, apparat.id, lower, upper)
        if evidence.print_signal_confirmed is not True:
            # SNMP didn't confirm printing (confident "no", or no data at all) -
            # check logs too: that's the only way to learn *why* (download error
            # vs something else), and it doubles as a cross-check against SNMP.
            filename_hint = await _find_document_filename(api, ticket.telegram_id, transaction.date)
            evidence.log_download_error, evidence.log_print_success = await _check_device_logs(
                api, apparat.id, lower, upper, filename_hint=filename_hint
            )

        # Cheap, real-time check first: PrintBox's own monitoring already
        # classifies device health continuously, so a current multi-apparat
        # outage or an active alert on this specific apparat is both a faster
        # and a more direct signal than re-deriving it by scanning neighbor
        # transactions' historical SNMP signal one by one.
        mass_outage_now, evidence.apparat_active_alert = await _check_current_health(api, apparat.id)
        if mass_outage_now:
            evidence.mass_outage_suspected = True
        else:
            # Nothing active right now doesn't mean nothing happened - the
            # incident may already be resolved, so still check what the
            # historical signal around the actual order looked like.
            window = settings.neighbor_window
            neighbor_idxs = list(range(max(0, idx - window), idx)) + list(
                range(idx + 1, min(len(pool), idx + 1 + window))
            )
            failures = 0
            checked = 0
            for ni in neighbor_idxs:
                n_bounds = _signal_bounds(pool, ni)
                if n_bounds is None:
                    # Part of a burst of simultaneous orders - unattributable,
                    # not failed. Counting these was what invented outages out
                    # of nothing more than a busy minute.
                    continue
                checked += 1
                ok = await _check_print_signal(api, apparat.id, *n_bounds)
                # Only a confident "no" counts - an unknown (None, no SNMP data
                # for that neighbor) shouldn't be mistaken for evidence of an outage.
                if ok is False:
                    failures += 1
            evidence.neighbor_failure_count = failures
            evidence.neighbor_total_checked = checked
            evidence.mass_outage_suspected = checked > 0 and failures >= 2

    return evidence


async def _check_current_health(api: PrintBoxAPIClient, apparat_id: int) -> tuple[bool, str | None]:
    """Cheap, real-time alternative/precursor to scanning neighbor transactions
    for a mass outage: PrintBox's own monitoring already classifies device
    health continuously via /printers/summary and /printers/alerts. Returns
    (mass_outage_suspected_now, this_apparat's own active alert message if
    any). Falls back to (False, None) - i.e. "fall back to the historical
    neighbor scan instead" - on an API error, or if nothing is currently
    active (a resolved-by-now incident still needs the historical check to
    be found at all).
    """
    try:
        summary = await api.get_printer_summary()
    except PrintBoxAPIError:
        return False, None
    if summary.get("offline", 0) >= 2 or summary.get("with_errors", 0) >= 2:
        return True, None

    try:
        alerts = await api.get_printer_alerts()
    except PrintBoxAPIError:
        return False, None
    mine = next(
        (a for a in alerts if a.get("apparat_id") == apparat_id and a.get("alert_level") in ("critical", "warning")),
        None,
    )
    return False, mine.get("message") if mine else None
