from log_parser import (
    entries_near,
    fix_mojibake,
    has_download_error,
    has_successful_print,
    parse_log_entries,
)
from datetime import datetime

# Real excerpt from a device's "логи.txt" (entries run together with no
# newlines, exactly as fetched from the device - this is what the parser
# actually has to handle in production).
SUCCESS_EXCERPT = (
    "2026-06-18 11:58:16,106 - INFO - Reading parameters from stdin"
    "2026-06-18 11:58:16,107 - INFO - Starting print process with URL:test.pdf"
    "2026-06-18 11:58:17,023 - INFO - Print process completed successfully"
)

ERROR_EXCERPT = (
    "2026-06-18 11:51:45,807 - INFO - Starting print process with URL:test.pdf"
    "2026-06-18 11:52:06,839 - ERROR - Unexpected error during download: "
    "HTTPSConnectionPool(host='nurtest.space', port=443): Max retries exceeded"
    "2026-06-18 11:52:06,840 - ERROR - Error in print process: HTTPSConnectionPool timeout"
)


def test_parses_entries_without_newline_separators():
    entries = parse_log_entries(SUCCESS_EXCERPT)
    assert len(entries) == 3
    assert entries[0].level == "INFO"
    assert entries[0].timestamp == datetime(2026, 6, 18, 11, 58, 16, 106000)
    assert "Reading parameters from stdin" in entries[0].message
    assert entries[-1].message == "Print process completed successfully"


def test_detects_successful_print():
    entries = parse_log_entries(SUCCESS_EXCERPT)
    assert has_successful_print(entries) is True
    assert has_download_error(entries) is False


def test_detects_download_error():
    entries = parse_log_entries(ERROR_EXCERPT)
    assert has_download_error(entries) is True
    assert has_successful_print(entries) is False


def test_entries_near_filters_by_window():
    entries = parse_log_entries(SUCCESS_EXCERPT + ERROR_EXCERPT)
    around = datetime(2026, 6, 18, 11, 58, 16)
    nearby = entries_near(entries, around, window_minutes=2)
    # ERROR_EXCERPT entries are ~6 minutes earlier (11:51-11:52) - outside the
    # ±2min window, so only the 3 SUCCESS_EXCERPT entries should remain.
    assert len(nearby) == 3
    assert all(e.timestamp.minute == 58 for e in nearby)


def test_fix_mojibake_recovers_cyrillic():
    original = "Попытка скачать файл по URL"
    mojibake = original.encode("cp1251").decode("latin-1")
    assert fix_mojibake(mojibake) == original


def test_fix_mojibake_leaves_clean_ascii_untouched():
    text = "Print process completed successfully"
    assert fix_mojibake(text) == text
