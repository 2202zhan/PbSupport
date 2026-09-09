"""Phase 4: the tools that look things up.

The rule these enforce is structural, not editorial: the model is never shown a
toner percentage or a sheet count, so it cannot put one in a reply. The exact
readings go to ctx.staff_notes, which reaches the escalation card instead.
"""
from datetime import datetime, timedelta

import pytest

import apparats
import tz
from agent.reading import CHECK_APPARAT, FIND_MY_ORDERS, SERVICE_INFO
from agent.registry import ToolError
from agent.types import TurnContext
from api_client import Apparat, PrintBoxAPIError, Transaction


class _Api:
    def __init__(self, pages=283, toner=None, error=None, transactions=None, raise_=False):
        self.pages, self.toner, self.error = pages, toner or {"black": 51}, error
        self.transactions = transactions or []
        self.raise_ = raise_

    async def get_apparats(self):
        if self.raise_:
            raise PrintBoxAPIError("down")
        return [
            Apparat(id=3, name_apparat="Аппарат №3 3️⃣", address="Первый корпус",
                    status="online", pages_left=self.pages),
            Apparat(id=1, name_apparat="Аппарат №1", address="Главный корпус, 1 этаж",
                    status="online", pages_left=400),
        ]

    async def get_all_printer_statuses(self):
        if self.raise_:
            raise PrintBoxAPIError("down")
        return [
            {"apparat_id": 3, "is_online": True, "toner": self.toner, "error_text": self.error},
            {"apparat_id": 1, "is_online": True, "toner": {"black": 74}, "error_text": None},
        ]

    async def get_transactions(self, telegram_id=None, per_page=20):
        if self.raise_:
            raise PrintBoxAPIError("down")
        return self.transactions


def _ctx(api):
    return TurnContext("884013433", "zhan", 1, "не печатает", api=api)


@pytest.fixture(autouse=True)
def daytime(monkeypatch):
    monkeypatch.setattr(apparats.tz, "now", lambda: datetime(2026, 9, 9, 14, 0))


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    # investigate_order reads the conversation for a stored receipt; without
    # this the suite would reach for the running bot's own database.
    monkeypatch.setattr("storage.settings.support_bot_db_path", str(tmp_path / "t.sqlite3"))
    import storage

    storage.init_db()


async def test_service_facts_come_from_the_curated_list():
    answer = await SERVICE_INFO.run({"topics": ["hours"]}, None)
    assert "8:00" in answer["hours"] and "19:00" in answer["hours"]


async def test_several_facts_come_back_in_one_call():
    # One topic per model call is how "как у вас печатать?" ran out of budget
    # halfway through its own answer.
    answer = await SERVICE_INFO.run({"topics": ["hours", "formats", "how_it_works"]}, None)
    assert set(answer) == {"hours", "formats", "how_it_works"}


async def test_an_unknown_topic_is_worth_correcting():
    with pytest.raises(ToolError):
        await SERVICE_INFO.run({"topics": ["погода"]}, None)
    with pytest.raises(ToolError):
        await SERVICE_INFO.run({}, None)


async def test_we_do_not_invent_a_price():
    # There is no price table anywhere in this codebase, so the honest answer
    # is where to look, not a number.
    answer = await SERVICE_INFO.run({"topics": ["prices"]}, None)
    assert "не называй цифры" in answer["prices"]


async def test_the_model_is_told_the_verdict_and_never_the_numbers():
    ctx = _ctx(_Api())
    answer = await CHECK_APPARAT.run({"place": "аппарат 3"}, ctx)
    rendered = str(answer)
    assert "%" not in rendered and "283" not in rendered
    assert "не сообщает" in answer["состояние"]
    # The reading itself is not lost - it goes where a person can use it.
    assert "283" in ctx.staff_notes[0] and "51%" in ctx.staff_notes[0]
    assert "аппарат 3" in ctx.staff_notes[0]  # what was searched for, so a mismatch shows


async def test_the_machine_is_named_the_way_our_records_name_it():
    # The model paraphrases what the user said; reporting a reading from one
    # kiosk under another kiosk's name is how it confidently misleads.
    ctx = _ctx(_Api())
    answer = await CHECK_APPARAT.run({"place": "аппарат 3"}, ctx)
    assert answer["аппарат"] == "Аппарат №3 3️⃣ (Первый корпус)"


async def test_a_place_nobody_named_gets_asked_about_not_guessed():
    ctx = _ctx(_Api())
    answer = await CHECK_APPARAT.run({}, ctx)
    assert "наши_точки" in answer
    assert "Первый корпус" in answer["наши_точки"]
    assert ctx.staff_notes == []  # nothing was read, so nothing is reported


async def test_an_unrecognised_place_offers_the_real_ones():
    ctx = _ctx(_Api())
    answer = await CHECK_APPARAT.run({"place": "у общаги"}, ctx)
    assert "наши_точки" in answer and answer["наши_точки"]


async def test_a_low_machine_is_flagged_for_intervention():
    ctx = _ctx(_Api(pages=3, toner={"black": 4}))
    answer = await CHECK_APPARAT.run({"place": "аппарат 3"}, ctx)
    assert answer["требует_вмешательства"] is True
    assert "рядом_работает" in answer  # somewhere to send them meanwhile


async def test_a_healthy_machine_is_not_flagged():
    ctx = _ctx(_Api())
    answer = await CHECK_APPARAT.run({"place": "аппарат 3"}, ctx)
    assert answer["требует_вмешательства"] is False


async def test_at_night_the_answer_is_that_they_are_off(monkeypatch):
    monkeypatch.setattr(apparats.tz, "now", lambda: datetime(2026, 9, 9, 23, 58))
    ctx = _ctx(_Api())
    answer = await CHECK_APPARAT.run({"place": "аппарат 3"}, ctx)
    assert "выключены" in answer["состояние"]


def _transaction(minutes_ago, amount, tx_id="t1"):
    return Transaction(
        id=tx_id, date=tz.now() - timedelta(minutes=minutes_ago), machine="Аппарат №3",
        user="@zhan", telegram_id="884013433", amount=amount, status="completed",
        payment_method="kaspi", print_type="bw",
    )


async def test_orders_come_back_in_words_a_person_would_use():
    ctx = _ctx(_Api(transactions=[_transaction(3, 80), _transaction(60 * 26, 40, "t2")]))
    answer = await FIND_MY_ORDERS.run({}, ctx)
    whens = [o["когда"] for o in answer["список"]]
    assert whens[0] == "3 мин назад"
    assert "вчера" in whens[1]
    # The user's own money is theirs to see - unlike our toner counters.
    assert answer["список"][0]["сумма"] == "80 ₸"


async def test_no_orders_says_so_and_suggests_the_next_step():
    answer = await FIND_MY_ORDERS.run({}, _ctx(_Api()))
    assert answer["заказов"] == 0
    assert "чек" in answer["вывод"]


async def test_order_ids_reach_staff_but_not_the_model():
    ctx = _ctx(_Api(transactions=[_transaction(3, 80, "63192")]))
    answer = await FIND_MY_ORDERS.run({}, ctx)
    assert "63192" not in str(answer)
    assert "63192" in ctx.staff_notes[0]


async def test_a_broken_api_says_so_instead_of_pretending():
    answer = await FIND_MY_ORDERS.run({}, _ctx(_Api(raise_=True)))
    assert "ошибка" in answer

    ctx = _ctx(_Api(raise_=True))
    apparat_answer = await CHECK_APPARAT.run({"place": "аппарат 3"}, ctx)
    assert "нужно_уточнить" in apparat_answer or "показаний" in apparat_answer["состояние"]


class _EvidenceApi(_Api):
    """Enough of the API for gather_evidence to run without the network."""

    async def get_printer_history(self, apparat_id, limit=50, offset=0):
        return []

    async def get_printer_alerts(self):
        return []

    async def get_printer_summary(self):
        return {}

    async def get_telegram_documents(self, telegram_id):
        return []

    async def request_device_logs(self, apparat_id, lines=1000, log_type="print"):
        return None

    async def get_device_logs(self, apparat_id, log_type="print"):
        return ""


def _evidence(**overrides):
    from diagnosis import Evidence, TicketInput

    ticket = TicketInput(
        problem_type="not_printed", apparat_name_text="Аппарат №3", telegram_id="884013433",
        username="zhan", contact=None, raw_text="не вышло", submitted_at=tz.now(),
    )
    defaults = dict(
        ticket=ticket, identity_confirmed=True, transaction=_transaction(5, 80, "63192"),
        apparat=None,
    )
    defaults.update(overrides)
    return Evidence(**defaults)


async def _investigate(monkeypatch, evidence, ctx=None):
    from agent.reading import INVESTIGATE_ORDER

    async def _gather(_api, _ticket):
        return evidence

    monkeypatch.setattr("diagnosis.gather_evidence", _gather)
    ctx = ctx or _ctx(_EvidenceApi())
    return await INVESTIGATE_ORDER.run({"when": "только что"}, ctx), ctx


async def test_the_technical_verdict_reaches_the_model_in_plain_words(monkeypatch):
    answer, _ = await _investigate(
        monkeypatch, _evidence(print_signal_confirmed=False, log_download_error=True)
    )
    assert "не смог скачать файл" in answer["что_показала_техника"]
    assert answer["возврат_возможен"] is True


async def test_our_instruments_are_not_named_to_the_model(monkeypatch):
    answer, ctx = await _investigate(
        monkeypatch, _evidence(print_signal_confirmed=False, log_download_error=True)
    )
    rendered = str(answer).lower()
    for internal in ("snmp", "log_", "print_signal", "лог"):
        assert internal not in rendered, internal
    # They are exactly what a person needs, so they go on the card.
    assert "SNMP" in ctx.staff_notes[0]


async def test_a_successful_print_is_reported_as_such(monkeypatch):
    answer, _ = await _investigate(
        monkeypatch, _evidence(print_signal_confirmed=True, log_print_success=True)
    )
    assert "прошла до конца" in answer["что_показала_техника"]


async def test_a_blocked_refund_says_why_without_jargon(monkeypatch):
    answer, _ = await _investigate(monkeypatch, _evidence(already_refunded=True))
    assert answer["возврат_возможен"] is False
    assert any("возврат уже делали" in b for b in answer["мешает"])
    assert "сам возврат не предлагай" in answer["как_быть"]


async def test_no_payment_found_asks_for_more_instead_of_guessing(monkeypatch):
    answer, _ = await _investigate(monkeypatch, _evidence(transaction=None))
    assert answer["оплата_найдена"] is False
    assert "чек" in answer["вывод"]


async def test_a_mass_outage_is_flagged_to_staff(monkeypatch):
    _, ctx = await _investigate(
        monkeypatch,
        _evidence(print_signal_confirmed=False, mass_outage_suspected=True,
                  neighbor_failure_count=5, neighbor_total_checked=7),
    )
    assert "массовый сбой" in ctx.staff_notes[0]


async def test_the_user_is_told_the_check_is_running(monkeypatch):
    said = []

    async def _progress(text):
        said.append(text)

    ctx = _ctx(_EvidenceApi())
    ctx.on_progress = _progress
    await _investigate(monkeypatch, _evidence(), ctx=ctx)
    assert said and "Проверяю" in said[0]


async def test_a_broken_investigation_calls_for_a_human(monkeypatch):
    from agent.reading import INVESTIGATE_ORDER

    async def _boom(_api, _ticket):
        raise PrintBoxAPIError("down")

    monkeypatch.setattr("diagnosis.gather_evidence", _boom)
    answer = await INVESTIGATE_ORDER.run({}, _ctx(_EvidenceApi()))
    assert "зови человека" in answer["ошибка"]
