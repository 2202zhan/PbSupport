import re
from dataclasses import dataclass
from datetime import datetime, timedelta

_MOJIBAKE_CHARS = set("àáâãäåæçèéêëìíîïðñòóôõöùúûüýÿÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖÙÚÛÜÝ")

_ENTRY_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s*-\s*"
    r"(?P<level>INFO|ERROR|WARNING|DEBUG)\s*-\s*"
)

_TS_FORMAT = "%Y-%m-%d %H:%M:%S,%f"


@dataclass
class LogEntry:
    timestamp: datetime
    level: str
    message: str


def fix_mojibake(text: str) -> str:
    """Restores Cyrillic that was CP1251-encoded but decoded as Latin-1.

    Device PC loggers write CP1251; somewhere downstream the bytes get redecoded as
    Latin-1, turning Cyrillic into garbled accented Latin letters (e.g. "îïûòêà").
    Markers we actually key decisions on (ERROR/INFO/"Print process completed
    successfully") are plain ASCII and unaffected either way - this is only for
    producing a readable log when escalating to a human.
    """
    if not text:
        return text
    mojibake_density = sum(1 for ch in text if ch in _MOJIBAKE_CHARS) / len(text)
    if mojibake_density < 0.05:
        return text
    try:
        repaired = text.encode("latin-1").decode("cp1251")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    if not any("Ѐ" <= ch <= "ӿ" for ch in repaired):
        return text
    return repaired


def parse_log_entries(raw_content: str) -> list[LogEntry]:
    """Splits raw device-log content into entries.

    Real PC logs are not reliably newline-delimited (entries can run together), so
    entries are split on the timestamp+level prefix pattern instead of on "\\n".
    """
    matches = list(_ENTRY_RE.finditer(raw_content))
    entries: list[LogEntry] = []
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw_content)
        message = raw_content[start:end].strip()
        try:
            timestamp = datetime.strptime(match.group("ts"), _TS_FORMAT)
        except ValueError:
            continue
        entries.append(
            LogEntry(timestamp=timestamp, level=match.group("level"), message=fix_mojibake(message))
        )
    return entries


def entries_near(entries: list[LogEntry], around: datetime, window_minutes: int = 5) -> list[LogEntry]:
    delta = timedelta(minutes=window_minutes)
    return [e for e in entries if around - delta <= e.timestamp <= around + delta]


def entries_between(entries: list[LogEntry], lower: datetime, upper: datetime) -> list[LogEntry]:
    return [e for e in entries if lower <= e.timestamp <= upper]


def has_download_error(entries: list[LogEntry]) -> bool:
    return any(e.level == "ERROR" and "download" in e.message.lower() for e in entries)


def has_successful_print(entries: list[LogEntry]) -> bool:
    return any("print process completed successfully" in e.message.lower() for e in entries)
