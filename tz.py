"""All datetimes in this codebase are naive, representing Asia/Almaty wall-clock
time - confirmed empirically: the PrintBox API returns timestamps like
"2026-06-18T14:30:01.093784" (no offset, no "Z"), and device PC logs use the same
naive local-clock convention. Use `now()` here instead of `datetime.now()` /
`datetime.now(timezone.utc)` so our own timestamps line up with API/log data
without any conversion.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

ALMATY = ZoneInfo("Asia/Almaty")


def now() -> datetime:
    return datetime.now(ALMATY).replace(tzinfo=None)
