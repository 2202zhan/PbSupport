import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import storage
import triage


def test_custom_when_indicates_old_for_explicit_day_counts():
    assert triage._custom_when_indicates_old("2 дня назад") is True
    assert triage._custom_when_indicates_old("3 дней назад, в обед") is True


def test_custom_when_indicates_old_for_relative_words():
    assert triage._custom_when_indicates_old("вчера утром") is True
    assert triage._custom_when_indicates_old("позавчера вечером") is True
    assert triage._custom_when_indicates_old("на прошлой неделе") is True
    assert triage._custom_when_indicates_old("в прошлом месяце") is True


def test_custom_when_indicates_old_false_for_recent_phrasing():
    assert triage._custom_when_indicates_old("только что") is False
    assert triage._custom_when_indicates_old("20 минут назад") is False
    assert triage._custom_when_indicates_old("сегодня утром") is False


def test_is_too_old_true_for_receipt_from_two_days_ago(monkeypatch):
    now = datetime(2026, 6, 20, 23, 55, 0)
    monkeypatch.setattr(triage.tz, "now", lambda: now)
    receipt_paid_at = datetime(2026, 6, 18, 11, 58, 12)  # ~2.5 days earlier
    assert triage._is_too_old(receipt_paid_at) is True


def test_is_too_old_false_for_recent_time(monkeypatch):
    now = datetime(2026, 6, 20, 23, 55, 0)
    monkeypatch.setattr(triage.tz, "now", lambda: now)
    assert triage._is_too_old(now - timedelta(hours=2)) is False


def test_is_too_old_false_when_no_hint_time():
    assert triage._is_too_old(None) is False


def test_build_intake_summary_includes_sub_issue_fields():
    summary = triage._build_intake_summary(
        "payment_error",
        {"payment_issue": "🏦 Ошибка в приложении банка при оплате", "when_label": "🕐 10–30 минут назад"},
    )
    assert "Ошибка в приложении банка" in summary
    assert "10–30 минут назад" in summary


def test_build_intake_summary_prefers_parsed_receipt_over_bucket():
    summary = triage._build_intake_summary(
        "not_printed",
        {
            "situation": "Оплатил(а), но распечатка не вышла",
            "receipt_amount": 35.0,
            "receipt_paid_at": datetime(2026, 6, 18, 11, 58, 12),
            "amount_label": "до 50 ₸",  # should not appear - receipt takes precedence
        },
    )
    assert "35 ₸" in summary
    assert "18.06.2026 11:58:12" in summary
    assert "до 50" not in summary


def test_build_intake_summary_upload_issue():
    summary = triage._build_intake_summary("upload_failed", {"upload_issue": "🔢 Код не пришёл"})
    assert "Код не пришёл" in summary


def test_parse_amount_text_extracts_plain_number():
    assert triage._parse_amount_text("35") == 35.0
    assert triage._parse_amount_text("35 тенге") == 35.0


def test_parse_amount_text_handles_decimal_and_thousands_separators():
    assert triage._parse_amount_text("35.50") == 35.5
    assert triage._parse_amount_text("1 250,50") == 1250.5


def test_parse_amount_text_returns_none_when_no_number():
    assert triage._parse_amount_text("не помню сколько") is None


def test_payment_error_options_no_longer_include_removed_keys():
    # wrong_amount/qr_not_recognized were removed - only these four remain.
    option_keys = {key for key, _ in triage._PAYMENT_ERROR_TYPE_OPTIONS}
    assert option_keys == {"paid_not_printed", "qr_not_shown", "bank_error", "custom"}


def test_upload_issue_options_no_longer_include_not_arrived():
    option_keys = {key for key, _ in triage._UPLOAD_ISSUE_OPTIONS}
    assert option_keys == {"upload_error", "no_code", "custom"}


def test_mentions_payment_or_print_true_for_relevant_keywords():
    assert triage._mentions_payment_or_print("деньги списались, а заказ не пришёл") is True
    assert triage._mentions_payment_or_print("аппарат не печатает второй день") is True
    assert triage._mentions_payment_or_print("оплатил 100 тенге, ничего не вышло") is True


def test_mentions_payment_or_print_false_for_unrelated_text():
    assert triage._mentions_payment_or_print("где находится второй корпус?") is False
    assert triage._mentions_payment_or_print("как зовут поддержку, можно узнать имя?") is False


async def test_check_qr_payment_availability_true_when_apparat_online():
    class FakeApi:
        async def get_apparats(self):
            return [_FakeApparat(id=1, name_apparat="Аппарат №1")]

        async def get_all_printer_statuses(self):
            return [{"apparat_id": 1, "is_online": True, "error_text": None}]

    assert await triage._check_qr_payment_availability(FakeApi(), "Аппарат №1") is True


async def test_check_qr_payment_availability_false_when_apparat_offline():
    class FakeApi:
        async def get_apparats(self):
            return [_FakeApparat(id=1, name_apparat="Аппарат №1")]

        async def get_all_printer_statuses(self):
            return [{"apparat_id": 1, "is_online": False, "error_text": "offline"}]

    assert await triage._check_qr_payment_availability(FakeApi(), "Аппарат №1") is False


async def test_check_qr_payment_availability_false_when_apparat_unknown():
    class FakeApi:
        async def get_apparats(self):
            return []

        async def get_all_printer_statuses(self):
            return []

    assert await triage._check_qr_payment_availability(FakeApi(), "Аппарат №99") is False


def test_problem_labels_include_device_issue_not_just_personal_complaints():
    option_keys = set(triage._PROBLEM_LABELS)
    assert option_keys == {
        "upload_failed", "not_printed", "payment_error", "print_quality", "device_issue", "other",
    }


def test_quality_replies_never_ask_the_user_to_service_the_machine():
    # The user is standing at a kiosk that isn't theirs - telling them to change
    # a cartridge or clean a drum is both useless and insulting.
    forbidden = ["картридж", "почист", "замен", "барабан", "перезагруз"]
    for key, reply in triage._QUALITY_REPLIES.items():
        lowered = reply.lower()
        assert not any(word in lowered for word in forbidden), key


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

    result = await triage._suggest_alternate_apparat(FakeApi(), "Аппарат №1")
    assert result is not None
    assert "Аппарат №2" in result


async def test_suggest_alternate_apparat_none_when_nothing_else_online():
    class FakeApi:
        async def get_apparats(self):
            return [_FakeApparat(id=1, name_apparat="Аппарат №1")]

        async def get_all_printer_statuses(self):
            return [{"apparat_id": 1, "is_online": False, "error_text": "бумага закончилась"}]

    assert await triage._suggest_alternate_apparat(FakeApi(), "Аппарат №1") is None


def test_escalation_receipt_keyboard_has_photo_and_skip_buttons():
    keyboard = triage._escalation_receipt_keyboard()
    callback_data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert callback_data == ["escreceipt:photo", "escreceipt:skip"]


class _FakeApparat:
    def __init__(self, id, name_apparat):
        self.id = id
        self.name_apparat = name_apparat
        self.address = ""
        self.status = "online"


def test_resolve_phone_input_prefers_shared_contact():
    assert triage._resolve_phone_input("+77001234567", "не подставится") == "+77001234567"


def test_resolve_phone_input_accepts_typed_number():
    assert triage._resolve_phone_input(None, "+77001234567") == "+77001234567"
    assert triage._resolve_phone_input(None, "8 700 123 45 67") == "8 700 123 45 67"


def test_resolve_phone_input_none_when_not_a_phone():
    # Mandatory - no skip phrase accepted, these should all fail to resolve and
    # cause the caller to re-ask instead of escalating with no contact at all.
    assert triage._resolve_phone_input(None, "нет") is None
    assert triage._resolve_phone_input(None, "не хочу давать номер") is None
    assert triage._resolve_phone_input(None, "123") is None  # too few digits


def test_resolve_phone_input_none_when_nothing_provided():
    assert triage._resolve_phone_input(None, None) is None
    assert triage._resolve_phone_input(None, "") is None


def test_phone_request_keyboard_has_only_contact_button():
    keyboard = triage._phone_request_keyboard()
    buttons = [b for row in keyboard.keyboard for b in row]
    assert len(buttons) == 1
    assert buttons[0].request_contact is True


def test_is_admin_true_for_listed_id(monkeypatch):
    monkeypatch.setattr(triage.settings, "admin_telegram_ids", "111, 222 ,333")
    assert triage._is_admin("222") is True


def test_is_admin_false_for_unlisted_id(monkeypatch):
    monkeypatch.setattr(triage.settings, "admin_telegram_ids", "111,222")
    assert triage._is_admin("999") is False


def test_is_admin_false_when_unset(monkeypatch):
    monkeypatch.setattr(triage.settings, "admin_telegram_ids", "")
    assert triage._is_admin("123") is False


class _FakeMessage:
    def __init__(self, text, user_id=123):
        self.text = text
        self.from_user = SimpleNamespace(id=user_id, username="user")
        self.answered_with: list[str] = []

    async def answer(self, text, reply_markup=None):
        self.answered_with.append(text)


async def test_unknown_slash_command_gets_canned_reply_without_ai_call(monkeypatch):
    called = False

    async def _fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        return "should not be used"

    monkeypatch.setattr(triage.concierge, "answer", _fail_if_called)
    message = _FakeMessage("/privet")

    await triage.on_unstructured_message(message)

    assert called is False
    assert message.answered_with == [triage._UNKNOWN_COMMAND_REPLY]


async def test_plain_text_still_goes_to_concierge(monkeypatch):
    async def _fake_answer(text):
        return f"echo: {text}"

    monkeypatch.setattr(triage.concierge, "answer", _fake_answer)
    message = _FakeMessage("сколько стоит цветная печать?")

    await triage.on_unstructured_message(message)

    assert message.answered_with == ["echo: сколько стоит цветная печать?"]


def test_clip_free_text_leaves_short_text_untouched():
    assert triage._clip_free_text("обычный текст") == "обычный текст"


def test_clip_free_text_truncates_huge_paste():
    huge = "а" * 10_000
    clipped = triage._clip_free_text(huge)
    assert len(clipped) == triage._FREE_TEXT_LIMIT


async def test_apparat_named_handles_voice_instead_of_text():
    # Voice/sticker/etc instead of typed text used to crash downstream
    # parsing (_custom_when_indicates_old/_parse_amount_text on None).
    message = _FakeMessage(None)
    await triage.on_apparat_named(message, state=None)
    assert message.answered_with == ["Пожалуйста, напишите название/место аппарата текстом."]


async def test_intake_custom_answer_handles_voice_instead_of_text():
    message = _FakeMessage(None)
    await triage.on_intake_custom_answer(message, state=None, bot=None, api=None)
    assert message.answered_with == ["Пожалуйста, напишите ответ текстом."]


async def test_description_handles_voice_instead_of_text():
    message = _FakeMessage(None)
    await triage.on_description(message, state=None, bot=None, api=None)
    assert message.answered_with == ["Пожалуйста, опишите проблему текстом."]


async def test_nothelped_detail_handles_voice_instead_of_text():
    message = _FakeMessage(None)
    await triage.on_nothelped_detail_provided(message, state=None, bot=None)
    assert message.answered_with == ["Пожалуйста, напишите текстом, что именно не так."]


def test_every_user_facing_prompt_forbids_self_service_advice():
    # The regression this guards: a model with no service context told a student
    # standing at our kiosk to replace the cartridge, clean the drum, and call a
    # paper supplier. Each prompt that writes to the user must carry the limits.
    import ai_decider
    import concierge

    for name, prompt in [
        ("decide", ai_decider._SYSTEM_PROMPT),
        ("decide_followup", ai_decider._FOLLOWUP_SYSTEM_PROMPT),
        ("concierge", concierge._SYSTEM_PROMPT),
    ]:
        assert "картридж" in prompt, name
        assert "НИКОГДА не советуй" in prompt, name


class _FakeStatusMessage:
    def __init__(self):
        self.texts: list[str] = []

    async def edit_text(self, text, reply_markup=None):
        self.texts.append(text)


class _RecordingMessage(_FakeMessage):
    """_FakeMessage whose answer() returns something editable, since the
    follow-up handler narrates progress into the message it just sent."""

    async def answer(self, text, reply_markup=None):
        self.answered_with.append(text)
        return _FakeStatusMessage()


class _FakeState:
    def __init__(self, data):
        self._data = dict(data)
        self.state = None

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kwargs):
        self._data.update(kwargs)

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self._data.clear()


def _record(**overrides) -> storage.TicketRecord:
    defaults = dict(
        id=1, telegram_id="123", username="u", contact="+77000000000",
        problem_type="payment_error", apparat_name="Аппарат №1",
        raw_text="QR-код не появился на экране", transaction_id=None, status="escalated",
        draft_reply=None, forum_topic_id=None, payment_expected=1, live_chat=0,
        created_at="2026-09-08T00:00:00",
    )
    defaults.update(overrides)
    return storage.TicketRecord(**defaults)


def _escalating_followup(monkeypatch, ticket):
    monkeypatch.setattr(triage, "_STAGE_PAUSE_SECONDS", 0)

    async def _decide(**_):
        return triage.ai_decider.FollowupDecision(action="escalate", reason="r", staff_summary="s")

    async def _get_ticket(_id):
        return ticket

    monkeypatch.setattr(triage.ai_decider, "decide_followup", _decide)
    monkeypatch.setattr(triage.storage, "get_ticket", _get_ticket)


async def test_pre_payment_complaint_skips_the_receipt_question(monkeypatch):
    # "QR не появился" means no payment happened, so no receipt can exist -
    # asking for one reads as not having listened to the complaint.
    _escalating_followup(monkeypatch, _record(payment_expected=0))
    reached_phone = []

    async def _proceed(message, state, bot, telegram_id):
        reached_phone.append(True)

    monkeypatch.setattr(triage, "_proceed_after_escalation_receipt", _proceed)

    message = _RecordingMessage("оплата кюар ыстемид")
    await triage.on_nothelped_detail_provided(
        message, _FakeState({"ticket_id": 1, "pending_feedback_original_reply": "…"}), None
    )

    assert reached_phone == [True]
    assert not any("чек" in t.lower() for t in message.answered_with)


async def test_paid_complaint_still_asks_for_the_receipt(monkeypatch):
    _escalating_followup(monkeypatch, _record(problem_type="not_printed", payment_expected=1))

    message = _RecordingMessage("деньги списались, ничего не вышло")
    await triage.on_nothelped_detail_provided(
        message, _FakeState({"ticket_id": 1, "pending_feedback_original_reply": "…"}), None
    )

    assert any("чек" in t.lower() for t in message.answered_with)


class _TonerApi:
    def __init__(self, toner=None, raise_=False):
        self.toner, self.raise_ = toner, raise_

    async def get_apparats(self):
        if self.raise_:
            from api_client import PrintBoxAPIError
            raise PrintBoxAPIError("down")
        return [_FakeApparat(id=1, name_apparat="Аппарат №1")]

    async def get_all_printer_statuses(self):
        if self.raise_:
            from api_client import PrintBoxAPIError
            raise PrintBoxAPIError("down")
        return [{"apparat_id": 1, "is_online": True, "error_text": None, "toner": self.toner}]


async def test_low_toner_is_reported_to_staff():
    reply, tell_staff = await triage._faded_print_reply(_TonerApi({"black": 4}), "Аппарат №1")
    assert tell_staff is True
    assert "4%" in reply


async def test_healthy_toner_is_not_reported_to_staff():
    reply, tell_staff = await triage._faded_print_reply(_TonerApi({"black": 80}), "Аппарат №1")
    assert tell_staff is False
    assert "80%" in reply


async def test_unreadable_toner_does_not_guess():
    reply, tell_staff = await triage._faded_print_reply(_TonerApi(raise_=True), "Аппарат №1")
    assert tell_staff is False
    assert "не могу проверить" in reply


async def test_only_the_branch_that_escalates_claims_it_did():
    # The recurring bug: a scripted reply saying "передал сотрудникам" when
    # _send_scripted_reply notifies nobody, leaving the user waiting.
    for api, should_notify in [(_TonerApi({"black": 4}), True), (_TonerApi({"black": 80}), False),
                               (_TonerApi(raise_=True), False)]:
        reply, tell_staff = await triage._faded_print_reply(api, "Аппарат №1")
        claims = "передаю сотрудник" in reply.lower() or "передал сотрудник" in reply.lower()
        assert claims == should_notify, reply
    for key, reply in triage._QUALITY_REPLIES.items():
        assert "передал сотрудник" not in reply.lower(), key


async def test_user_message_goes_to_staff_while_live_chat_is_open(monkeypatch):
    # Without this the user answers a human and the assistant replies instead -
    # which is what happened when the relay was one message wide.
    relayed = []

    async def _relay(bot, ticket, message):
        relayed.append(message.text)

    async def _find(_tid):
        return _record(live_chat=1, forum_topic_id=42)

    monkeypatch.setattr(triage.notify, "relay_user_message", _relay)
    monkeypatch.setattr(triage.storage, "find_live_chat_ticket", _find)

    message = _RecordingMessage("а когда почините?")
    await triage.on_live_chat_message(message, bot=None)

    assert relayed == ["а когда почините?"]


async def test_without_live_chat_the_message_falls_through_to_the_assistant(monkeypatch):
    from aiogram.dispatcher.event.bases import SkipHandler

    async def _find(_tid):
        return None

    monkeypatch.setattr(triage.storage, "find_live_chat_ticket", _find)

    message = _RecordingMessage("сколько стоит цветная печать?")
    try:
        await triage.on_live_chat_message(message, bot=None)
    except SkipHandler:
        pass
    else:
        raise AssertionError("should have skipped to the next handler")


class _SuppliesApi:
    def __init__(self, pages=None, toner=None, error=None, raise_=False):
        self.pages, self.toner, self.error, self.raise_ = pages, toner, error, raise_

    async def get_apparats(self):
        if self.raise_:
            from api_client import PrintBoxAPIError
            raise PrintBoxAPIError("down")
        from api_client import Apparat
        return [Apparat(id=1, name_apparat="Аппарат №1", address="Главный",
                        status="online", pages_left=self.pages)]

    async def get_all_printer_statuses(self):
        if self.raise_:
            from api_client import PrintBoxAPIError
            raise PrintBoxAPIError("down")
        return [{"apparat_id": 1, "toner": self.toner, "error_text": self.error}]



def _daytime(monkeypatch):
    monkeypatch.setattr(triage.tz, "now", lambda: datetime(2026, 9, 8, 14, 0))


async def test_low_paper_corroborates_the_report(monkeypatch):
    _daytime(monkeypatch)
    verdict, staff = await triage._read_supplies(_SuppliesApi(pages=3, toner={"black": 60}), "Аппарат №1")
    assert verdict == "confirmed"
    assert "3 листа" in staff


async def test_device_error_counts_as_confirmation(monkeypatch):
    _daytime(monkeypatch)
    verdict, staff = await triage._read_supplies(
        _SuppliesApi(pages=400, toner={"black": 60}, error="Замялась бумага"), "Аппарат №1"
    )
    assert verdict == "confirmed"
    assert "Замялась бумага" in staff


async def test_healthy_readings_are_resolved_without_bothering_staff(monkeypatch):
    # The apparat reports its own faults through error_text - a clean reading
    # with full counters means there is nothing to carry over.
    _daytime(monkeypatch)
    verdict, staff = await triage._read_supplies(
        _SuppliesApi(pages=283, toner={"black": 51}), "Аппарат №1"
    )
    assert verdict == "healthy"
    assert "283 листа" in staff


async def test_readings_taken_at_night_mean_nothing(monkeypatch):
    # Kiosks are powered on 08:00-19:00; a status read at 02:00 says the
    # machine is off, not that it is broken.
    monkeypatch.setattr(triage.tz, "now", lambda: datetime(2026, 9, 8, 2, 0))
    verdict, staff = await triage._read_supplies(_SuppliesApi(pages=3), "Аппарат №1")
    assert verdict == "unknown"
    assert "выключены" in staff


async def test_unreadable_supplies_do_not_block_the_report(monkeypatch):
    _daytime(monkeypatch)
    verdict, staff = await triage._read_supplies(_SuppliesApi(raise_=True), "Аппарат №1")
    assert verdict == "unknown"
    assert "недоступны" in staff


async def test_staff_figures_never_reach_the_user(monkeypatch):
    # The user hears that we looked, not what our counters say.
    _daytime(monkeypatch)
    _, staff = await triage._read_supplies(_SuppliesApi(pages=3, toner={"black": 4}), "Аппарат №1")
    assert "3" in staff and "4%" in staff  # figures exist, for staff


def test_working_hours_boundaries(monkeypatch):
    for hour, awake in [(7, False), (8, True), (13, True), (18, True), (19, False), (23, False)]:
        monkeypatch.setattr(triage.tz, "now", lambda h=hour: datetime(2026, 9, 8, h, 30))
        assert triage._apparats_are_awake() is awake, hour


def test_sheet_counts_use_the_right_russian_form():
    assert triage._sheets_word(1) == "лист"
    assert triage._sheets_word(3) == "листа"
    assert triage._sheets_word(5) == "листов"
    assert triage._sheets_word(11) == "листов"
    assert triage._sheets_word(21) == "лист"


async def test_a_receipt_from_months_ago_is_refused_not_escalated(monkeypatch):
    # Reported case: the ticket said "сегодня", the attached receipt was from
    # June. It sailed through to staff as "нужна ручная проверка" - the age
    # check existed, but only on the normal intake path, not here.
    import receipt_parser

    async def _extract(bot, message):
        return "file-1", True, receipt_parser.ReceiptData(
            amount=35.0, paid_at=datetime(2026, 6, 18, 11, 58, 12), receipt_number="QR16068964665"
        )

    escalated = []

    async def _escalate(*args, **kwargs):
        escalated.append(True)

    monkeypatch.setattr(triage, "_extract_receipt_data", _extract)
    monkeypatch.setattr(triage, "_escalate", _escalate)
    monkeypatch.setattr(triage.tz, "now", lambda: datetime(2026, 9, 8, 14, 0))
    monkeypatch.setattr(triage.storage, "set_ticket_status", lambda *a: asyncio.sleep(0))

    message = _RecordingMessage("")
    state = _FakeState({"awaiting_post_diagnosis_receipt": True, "ticket_id": 58})
    await triage.on_receipt_received(message, state, bot=None, api=None)

    assert not escalated, "an out-of-window receipt must not reach staff"
    assert any("18.06.2026" in t for t in message.answered_with)


async def test_a_recent_receipt_still_triggers_a_second_look(monkeypatch):
    import receipt_parser

    async def _extract(bot, message):
        return "file-1", True, receipt_parser.ReceiptData(
            amount=35.0, paid_at=datetime(2026, 9, 8, 13, 40), receipt_number="QR1"
        )

    rechecked = []

    async def _cycle(*args, **kwargs):
        rechecked.append(True)

    async def _get_ticket(_id):
        return _record(problem_type="not_printed", apparat_name="Аппарат №1")

    monkeypatch.setattr(triage, "_extract_receipt_data", _extract)
    monkeypatch.setattr(triage, "_run_decision_cycle", _cycle)
    monkeypatch.setattr(triage.storage, "get_ticket", _get_ticket)
    monkeypatch.setattr(triage.tz, "now", lambda: datetime(2026, 9, 8, 14, 0))

    message = _RecordingMessage("")
    state = _FakeState({"awaiting_post_diagnosis_receipt": True, "ticket_id": 58})
    await triage.on_receipt_received(message, state, bot=None, api=None)

    assert rechecked == [True]


async def test_receipt_sent_in_reply_to_the_ai_question_is_processed(monkeypatch):
    # Reported case: the model asked "пришлите чек", the user sent a PDF, and
    # the bot repeated the question - the in_dialogue handler read message.text,
    # which is None for a document, so nothing new ever reached the model.
    import receipt_parser

    async def _extract(bot, message):
        return "file-1", True, receipt_parser.ReceiptData(
            amount=35.0, paid_at=datetime(2026, 9, 8, 20, 55), receipt_number="QR1"
        )

    seen = {}

    async def _cycle(bot, api, state, message, ticket_id, ticket_input):
        seen["input"] = ticket_input

    async def _get_ticket(_id):
        return _record(problem_type="not_printed", apparat_name="Аппарат №1")

    monkeypatch.setattr(triage, "_extract_receipt_data", _extract)
    monkeypatch.setattr(triage, "_run_decision_cycle", _cycle)
    monkeypatch.setattr(triage.storage, "get_ticket", _get_ticket)
    monkeypatch.setattr(triage.tz, "now", lambda: datetime(2026, 9, 8, 21, 11))

    message = _RecordingMessage("")
    state = _FakeState({"ticket_id": 60, "dialogue_history": ["user: толедим но шыкпады"]})
    await triage.on_followup_receipt(message, state, bot=None, api=None)

    ticket_input = seen["input"]
    assert ticket_input.manual_hint_amount == 35.0
    assert ticket_input.manual_hint_is_precise is True
    assert ticket_input.receipt_photo_file_id == "file-1"
    assert any("чек" in line for line in ticket_input.dialogue_history)


async def test_unparsable_receipt_in_dialogue_still_reaches_staff(monkeypatch):
    # A photo can't be read without OCR, but it must still be attached and the
    # investigation re-run rather than the question repeated.
    async def _extract(bot, message):
        return "photo-1", False, None

    seen = {}

    async def _cycle(bot, api, state, message, ticket_id, ticket_input):
        seen["input"] = ticket_input

    async def _get_ticket(_id):
        return _record(problem_type="not_printed")

    monkeypatch.setattr(triage, "_extract_receipt_data", _extract)
    monkeypatch.setattr(triage, "_run_decision_cycle", _cycle)
    monkeypatch.setattr(triage.storage, "get_ticket", _get_ticket)

    message = _RecordingMessage("")
    state = _FakeState({"ticket_id": 60, "dialogue_history": []})
    await triage.on_followup_receipt(message, state, bot=None, api=None)

    assert seen["input"].receipt_photo_file_id == "photo-1"
    assert seen["input"].manual_hint_is_precise is False
