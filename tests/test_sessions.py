from datetime import datetime, timedelta

import sessions


def setup_function() -> None:
    sessions._last_activity.clear()


def test_touch_then_pop_expired_before_timeout_returns_nothing(monkeypatch):
    now = datetime(2026, 6, 19, 12, 0, 0)
    monkeypatch.setattr(sessions.tz, "now", lambda: now)
    sessions.touch("123")

    monkeypatch.setattr(sessions.tz, "now", lambda: now + timedelta(minutes=5))
    assert sessions.pop_expired(timeout_minutes=10) == []


def test_pop_expired_after_timeout_returns_and_clears(monkeypatch):
    now = datetime(2026, 6, 19, 12, 0, 0)
    monkeypatch.setattr(sessions.tz, "now", lambda: now)
    sessions.touch("123")

    monkeypatch.setattr(sessions.tz, "now", lambda: now + timedelta(minutes=11))
    assert sessions.pop_expired(timeout_minutes=10) == ["123"]
    # Popped once - second call shouldn't return it again.
    assert sessions.pop_expired(timeout_minutes=10) == []


def test_forget_removes_without_waiting_for_expiry():
    sessions.touch("456")
    sessions.forget("456")
    assert sessions.pop_expired(timeout_minutes=0) == []


def test_multiple_sessions_tracked_independently(monkeypatch):
    now = datetime(2026, 6, 19, 12, 0, 0)
    monkeypatch.setattr(sessions.tz, "now", lambda: now)
    sessions.touch("a")
    monkeypatch.setattr(sessions.tz, "now", lambda: now + timedelta(minutes=5))
    sessions.touch("b")
    monkeypatch.setattr(sessions.tz, "now", lambda: now + timedelta(minutes=11))

    assert sessions.pop_expired(timeout_minutes=10) == ["a"]
