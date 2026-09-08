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


async def test_service_facts_come_from_the_curated_list():
    answer = await SERVICE_INFO.run({"topic": "hours"}, None)
    assert "8:00" in answer["факт"] and "19:00" in answer["факт"]


async def test_an_unknown_topic_is_worth_correcting():
    with pytest.raises(ToolError):
        await SERVICE_INFO.run({"topic": "погода"}, None)


async def test_we_do_not_invent_a_price():
    # There is no price table anywhere in this codebase, so the honest answer
    # is where to look, not a number.
    answer = await SERVICE_INFO.run({"topic": "prices"}, None)
    assert "не называй цифры" in answer["факт"]


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
