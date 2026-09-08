"""What a kiosk says about itself.

Shared by the menu bot and the agent, which is the point: both have to give the
same answer to "is this machine all right", and the figures behind that answer
must stay on the staff side of it.
"""
from datetime import datetime

import apparats


def _daytime(monkeypatch):
    monkeypatch.setattr(apparats.tz, "now", lambda: datetime(2026, 9, 8, 14, 0))


class _FakeApparat:
    def __init__(self, id, name_apparat):
        self.id = id
        self.name_apparat = name_apparat
        self.address = ""
        self.status = "online"


class _SuppliesApi:
    def __init__(self, pages=None, toner=None, error=None, raise_=False):
        self.pages, self.toner, self.error, self.raise_ = pages, toner, error, raise_

    async def get_apparats(self):
        if self.raise_:
            from api_client import PrintBoxAPIError
            raise PrintBoxAPIError("down")
        from api_client import Apparat
        return [Apparat(id=1, name_apparat="Аппарат №1", address="Главный корпус",
                        status="online", pages_left=self.pages)]

    async def get_all_printer_statuses(self):
        if self.raise_:
            from api_client import PrintBoxAPIError
            raise PrintBoxAPIError("down")
        return [{"apparat_id": 1, "toner": self.toner, "error_text": self.error}]


async def test_suggest_alternate_apparat_finds_other_online_one():
    class FakeApi:
        async def get_apparats(self):
            return [
                _FakeApparat(id=1, name_apparat="Аппарат №1"),
                _FakeApparat(id=2, name_apparat="Аппарат №2"),
            ]

        async def get_all_printer_statuses(self):
            return [
                {"apparat_id": 1, "is_online": False, "error_text": "бумага закончилась"},
                {"apparat_id": 2, "is_online": True, "error_text": None},
            ]

    result = await apparats.suggest_alternate(FakeApi(), "Аппарат №1")
    assert result is not None
    assert "Аппарат №2" in result


async def test_suggest_alternate_apparat_none_when_nothing_else_online():
    class FakeApi:
        async def get_apparats(self):
            return [_FakeApparat(id=1, name_apparat="Аппарат №1")]

        async def get_all_printer_statuses(self):
            return [{"apparat_id": 1, "is_online": False, "error_text": "бумага закончилась"}]

    assert await apparats.suggest_alternate(FakeApi(), "Аппарат №1") is None


async def test_low_paper_corroborates_the_report(monkeypatch):
    _daytime(monkeypatch)
    verdict, staff = await apparats.read_supplies(_SuppliesApi(pages=3, toner={"black": 60}), "Аппарат №1")
    assert verdict == "critical"
    assert "3 листа" in staff


async def test_device_error_counts_as_confirmation(monkeypatch):
    _daytime(monkeypatch)
    verdict, staff = await apparats.read_supplies(
        _SuppliesApi(pages=400, toner={"black": 60}, error="Замялась бумага"), "Аппарат №1"
    )
    assert verdict == "critical"
    assert "Замялась бумага" in staff


async def test_healthy_readings_are_resolved_without_bothering_staff(monkeypatch):
    # The apparat reports its own faults through error_text - a clean reading
    # with full counters means there is nothing to carry over.
    _daytime(monkeypatch)
    verdict, staff = await apparats.read_supplies(
        _SuppliesApi(pages=283, toner={"black": 51}), "Аппарат №1"
    )
    assert verdict == "healthy"
    assert "283 листа" in staff


async def test_readings_taken_at_night_mean_nothing(monkeypatch):
    # Kiosks are powered on 08:00-19:00; a status read at 02:00 says the
    # machine is off, not that it is broken.
    monkeypatch.setattr(apparats.tz, "now", lambda: datetime(2026, 9, 8, 2, 0))
    verdict, staff = await apparats.read_supplies(_SuppliesApi(pages=3), "Аппарат №1")
    assert verdict == "asleep"
    assert "выключены" in staff


async def test_unreadable_supplies_do_not_block_the_report(monkeypatch):
    _daytime(monkeypatch)
    verdict, staff = await apparats.read_supplies(_SuppliesApi(raise_=True), "Аппарат №1")
    assert verdict == "unknown"
    assert "недоступны" in staff


async def test_staff_figures_never_reach_the_user(monkeypatch):
    # The user hears that we looked, not what our counters say.
    _daytime(monkeypatch)
    _, staff = await apparats.read_supplies(_SuppliesApi(pages=3, toner={"black": 4}), "Аппарат №1")
    assert "3" in staff and "4%" in staff  # figures exist, for staff


def test_working_hours_boundaries(monkeypatch):
    for hour, awake in [(7, False), (8, True), (13, True), (18, True), (19, False), (23, False)]:
        monkeypatch.setattr(apparats.tz, "now", lambda h=hour: datetime(2026, 9, 8, h, 30))
        assert apparats.are_awake() is awake, hour


def test_sheet_counts_use_the_right_russian_form():
    assert apparats.sheets_word(1) == "лист"
    assert apparats.sheets_word(3) == "листа"
    assert apparats.sheets_word(5) == "листов"
    assert apparats.sheets_word(11) == "листов"
    assert apparats.sheets_word(21) == "лист"
