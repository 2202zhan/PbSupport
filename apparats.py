"""What a kiosk says about itself.

One home for it, because both the menu bot and the agent ask the same question
and must not answer it differently. Figures live on the staff side of every
return value: the user is told what a reading means, never what our counters
say.
"""

import logging

import diagnosis
import tz
from api_client import Apparat, PrintBoxAPIClient, PrintBoxAPIError

logger = logging.getLogger(__name__)

LOW_PAGES = 50
LOW_TONER = 20

# The kiosks are powered on 08:00-19:00 Asia/Almaty. Outside that they're off,
# so a status read says nothing about the machine - only that it is night.
WORKDAY_START_HOUR = 8
WORKDAY_END_HOUR = 19

ASLEEP, CRITICAL, HEALTHY, UNKNOWN = "asleep", "critical", "healthy", "unknown"


def are_awake() -> bool:
    return WORKDAY_START_HOUR <= tz.now().hour < WORKDAY_END_HOUR


def sheets_word(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return "листов"
    return {1: "лист", 2: "листа", 3: "листа", 4: "листа"}.get(n % 10, "листов")


async def read_supplies(api: PrintBoxAPIClient, apparat_name_text: str) -> tuple[str, str]:
    verdict, staff_note, _ = await read_supplies_detailed(api, apparat_name_text)
    return verdict, staff_note


async def read_supplies_detailed(
    api: PrintBoxAPIClient, apparat_name_text: str
) -> tuple[str, str, "Apparat | None"]:
    """Returns (verdict, staff_note, resolved apparat):

    - "asleep"   - outside working hours the kiosks are off, so there is
                   nothing to read and nothing to tell staff either;
    - "critical" - something is genuinely low, or the apparat reports an error;
    - "healthy"  - it says everything is in order;
    - "unknown"  - we asked during working hours and got no answer, which is
                   itself worth a human look.
    """
    if not are_awake():
        return ASLEEP, f"аппараты выключены (сейчас {tz.now():%H:%M}), показаний нет", None
    try:
        apparat = await diagnosis.find_apparat_by_name(api, apparat_name_text)
        statuses = await api.get_all_printer_statuses()
    except PrintBoxAPIError:
        logger.warning("could not read supplies for %s", apparat_name_text)
        return UNKNOWN, "показания аппарата сейчас недоступны", None
    if apparat is None:
        return UNKNOWN, "аппарат не найден в справочнике", None

    current = next((s for s in statuses if s.get("apparat_id") == apparat.id), None) or {}
    toner = {k: v for k, v in (current.get("toner") or {}).items() if isinstance(v, (int, float))}
    lowest_toner = min(toner.values()) if toner else None
    pages = apparat.pages_left
    error_text = current.get("error_text")

    findings = []
    if pages is not None:
        findings.append(f"{pages} {sheets_word(pages)} бумаги")
    if lowest_toner is not None:
        findings.append(f"тонер {lowest_toner}%")
    if error_text:
        findings.append(f"аппарат сообщает: {error_text}")
    if not findings:
        return UNKNOWN, "показаний от аппарата нет", apparat

    staff_note = ", ".join(findings)
    critical = bool(error_text) or (pages is not None and pages < LOW_PAGES) or (
        lowest_toner is not None and lowest_toner < LOW_TONER
    )
    return (CRITICAL if critical else HEALTHY), staff_note, apparat


async def suggest_alternate(api: PrintBoxAPIClient, apparat_name_text: str) -> str | None:
    """Another kiosk that is up right now, to offer as a stopgap. Best-effort:
    an API hiccup here must not cost the user their actual answer."""
    try:
        reported = await diagnosis.find_apparat_by_name(api, apparat_name_text)
        apparats = await api.get_apparats()
        statuses = {s.get("apparat_id"): s for s in await api.get_all_printer_statuses()}
    except PrintBoxAPIError:
        logger.warning("could not look up an alternate apparat for %s", apparat_name_text)
        return None
    for a in apparats:
        if reported is not None and a.id == reported.id:
            continue
        status = statuses.get(a.id)
        if status and status.get("is_online") and not status.get("error_text"):
            return f"{a.name_apparat} ({a.address})" if a.address else a.name_apparat
    return None


async def known_places(api: PrintBoxAPIClient) -> list[str]:
    """Where our kiosks actually stand, in the words people would use. Offered
    back to the agent so it asks with real options instead of guessing a
    building it has never been told about."""
    try:
        found = await api.get_apparats()
    except PrintBoxAPIError:
        logger.warning("could not list apparats")
        return []
    places = []
    for a in found:
        place = (a.address or "").split(",")[0].strip() or a.name_apparat
        if place and place.lower() not in {p.lower() for p in places}:
            places.append(place)
    return places
