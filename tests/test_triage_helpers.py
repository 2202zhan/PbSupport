import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import apparats
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
    assert triage.receipts.is_stale(receipt_paid_at) is True


def test_is_too_old_false_for_recent_time(monkeypatch):
    now = datetime(2026, 6, 20, 23, 55, 0)
    monkeypatch.setattr(triage.tz, "now", lambda: now)
    assert triage.receipts.is_stale(now - timedelta(hours=2)) is False


def test_is_too_old_false_when_no_hint_time():
    assert triage.receipts.is_stale(None) is False


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


class _FakeApparat:
    def __init__(self, id, name_apparat):
        self.id = id
        self.name_apparat = name_apparat
        self.address = ""
        self.status = "online"


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


def test_escalation_receipt_keyboard_has_photo_and_skip_buttons():
    keyboard = triage._escalation_receipt_keyboard()
    callback_data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert callback_data == ["escreceipt:photo", "escreceipt:skip"]


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
    await triage.on_nothelped_detail_provided(message, state=None, bot=None, api=None)
    assert len(message.answered_with) == 1
    assert "словами" in message.answered_with[0]


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
        message, _FakeState({"ticket_id": 1, "pending_feedback_original_reply": "…"}), None, None
    )

    assert reached_phone == [True]
    assert not any("чек" in t.lower() for t in message.answered_with)


async def test_paid_complaint_still_asks_for_the_receipt(monkeypatch):
    _escalating_followup(monkeypatch, _record(problem_type="not_printed", payment_expected=1))

    message = _RecordingMessage("деньги списались, ничего не вышло")
    await triage.on_nothelped_detail_provided(
        message, _FakeState({"ticket_id": 1, "pending_feedback_original_reply": "…"}), None, None
    )

    assert any("чек" in t.lower() for t in message.answered_with)


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


def _daytime(monkeypatch):
    monkeypatch.setattr(apparats.tz, "now", lambda: datetime(2026, 9, 8, 14, 0))


def _quality(key, verdict, staff_note="тонер 4%"):
    return triage._quality_reply(key, verdict, "Аппарат №1", staff_note)


def test_low_supplies_reach_staff_with_the_figures():
    reply, staff_summary = _quality("faded", "critical")
    assert "4%" in staff_summary
    assert "исходе" in reply


def test_a_healthy_machine_is_not_reported_to_staff():
    # "по всем пустякам не надо создавать заявку - мы и так знаем уровни и
    # всегда следим". A kiosk that just told us nothing is low is not news.
    reply, staff_summary = _quality("faded", "healthy")
    assert staff_summary is None
    assert "ещё раз" in reply
    assert "Не помогло" in reply  # the way back if the reprint fails too


def test_an_unreadable_machine_is_worth_a_human_look():
    reply, staff_summary = _quality("streaks", "unknown", staff_note="показаний от аппарата нет")
    assert staff_summary
    assert "не могу" not in reply.lower()


def test_no_quality_answer_leaks_our_counters():
    for key in ("faded", "streaks"):
        for verdict in ("critical", "healthy", "unknown"):
            reply, _ = _quality(key, verdict)
            assert "%" not in reply, (key, verdict, reply)


def test_only_the_answers_that_escalate_claim_a_handoff():
    # The recurring bug: a scripted reply saying "передаю сотрудникам" while
    # nobody is told. The claim and the escalation now move together.
    import ai_decider

    for key in ("faded", "streaks"):
        for verdict in ("critical", "healthy", "unknown"):
            reply, staff_summary = _quality(key, verdict)
            assert ai_decider.promises_a_handoff(reply) == (staff_summary is not None), (key, verdict)


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

    monkeypatch.setattr(triage.receipts, "extract", _extract)
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

    monkeypatch.setattr(triage.receipts, "extract", _extract)
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

    monkeypatch.setattr(triage.receipts, "extract", _extract)
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

    monkeypatch.setattr(triage.receipts, "extract", _extract)
    monkeypatch.setattr(triage, "_run_decision_cycle", _cycle)
    monkeypatch.setattr(triage.storage, "get_ticket", _get_ticket)

    message = _RecordingMessage("")
    state = _FakeState({"ticket_id": 60, "dialogue_history": []})
    await triage.on_followup_receipt(message, state, bot=None, api=None)

    assert seen["input"].receipt_photo_file_id == "photo-1"
    assert seen["input"].manual_hint_is_precise is False


async def test_a_receipt_sent_after_not_helped_is_read_not_refused(monkeypatch):
    # Answering "что именно не так?" with proof of payment used to hit the
    # "напишите текстом" guard - the user had sent the most relevant thing
    # they had and was told it didn't count.
    import receipt_parser

    ticket = _record(problem_type="print_quality", payment_expected=1)
    _escalating_followup(monkeypatch, ticket)

    async def _extract(bot, message):
        return "file-1", True, receipt_parser.ReceiptData(
            amount=35.0, paid_at=triage.tz.now(), receipt_number="QR1"
        )

    seen = {}

    async def _decide(**kwargs):
        seen.update(kwargs)
        return triage.ai_decider.FollowupDecision(action="escalate", reason="r", staff_summary="s")

    monkeypatch.setattr(triage.receipts, "extract", _extract)
    monkeypatch.setattr(triage.ai_decider, "decide_followup", _decide)

    message = _RecordingMessage(None)
    state = _FakeState({"ticket_id": 1, "pending_feedback_original_reply": "…"})
    await triage.on_nothelped_detail_receipt(message, state, None, None)

    assert not any("текстом" in t for t in message.answered_with)
    assert "чек" in seen["user_followup"].lower()
    assert state._data["pending_escalation_receipt_file_id"] == "file-1"


async def test_a_stale_receipt_after_not_helped_is_still_refused(monkeypatch):
    import receipt_parser

    async def _extract(bot, message):
        return "file-1", True, receipt_parser.ReceiptData(
            amount=35.0, paid_at=triage.tz.now() - timedelta(days=3), receipt_number="QR1"
        )

    refused = []

    async def _refuse(message, state, ticket_id, paid_at):
        refused.append(paid_at)

    monkeypatch.setattr(triage.receipts, "extract", _extract)
    monkeypatch.setattr(triage, "_refuse_stale_receipt", _refuse)

    await triage.on_nothelped_detail_receipt(
        _RecordingMessage(None),
        _FakeState({"ticket_id": 1, "pending_feedback_original_reply": "…"}),
        None,
        None,
    )
    assert len(refused) == 1


async def test_apparat_state_note_carries_no_figures(monkeypatch):
    _daytime(monkeypatch)
    ticket = _record(problem_type="print_quality", apparat_name="Аппарат №1")
    note = await triage._apparat_state_note(_SuppliesApi(pages=283, toner={"black": 80}), ticket)
    assert note and "%" not in note


async def test_apparat_state_note_is_silent_when_the_kiosks_are_off(monkeypatch):
    monkeypatch.setattr(triage.tz, "now", lambda: datetime(2026, 9, 8, 2, 0))
    ticket = _record(problem_type="print_quality", apparat_name="Аппарат №1")
    note = await triage._apparat_state_note(_SuppliesApi(pages=283), ticket)
    assert note and "выключены" in note


async def test_apparat_state_note_skipped_for_a_money_ticket(monkeypatch):
    _daytime(monkeypatch)
    ticket = _record(problem_type="not_printed", apparat_name="Аппарат №1")
    assert await triage._apparat_state_note(_SuppliesApi(pages=283), ticket) is None


class _FakeCallback:
    def __init__(self, data, user_id=884013433):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id, username="zhan")
        self.message = _RecordingMessage(None)
        self.message.edited: list[str] = []

        async def _edit(text, reply_markup=None):
            self.message.edited.append(text)

        self.message.edit_text = _edit
        self.answered = False

    async def answer(self, *_args, **_kwargs):
        self.answered = True


def _scripted(monkeypatch, escalated):
    async def _create_ticket(**kwargs):
        escalated.setdefault("created", []).append(kwargs)
        return 61

    async def _record_decision(*_args):
        return None

    async def _escalate(bot, ticket_id, evidence, decision, review=None):
        escalated.setdefault("cards", []).append(decision)

    monkeypatch.setattr(triage.storage, "create_ticket", _create_ticket)
    monkeypatch.setattr(triage.storage, "record_decision", _record_decision)
    monkeypatch.setattr(triage, "_escalate", _escalate)


async def test_a_healthy_machine_closes_the_quality_complaint_itself(monkeypatch):
    # "по всем пустякам не надо создавать заявку - мы и так знаем уровни".
    _daytime(monkeypatch)
    seen = {}
    _scripted(monkeypatch, seen)

    callback = _FakeCallback("quality:faded")
    await triage.on_quality_chosen(
        callback, _FakeState({"apparat_name_text": "главный корпус"}), None,
        _SuppliesApi(pages=283, toner={"black": 80}),
    )

    assert seen.get("cards") is None
    reply = callback.message.answered_with[0]
    assert "%" not in reply and "ещё раз" in reply


async def test_a_low_machine_reaches_staff(monkeypatch):
    _daytime(monkeypatch)
    seen = {}
    _scripted(monkeypatch, seen)

    callback = _FakeCallback("quality:streaks")
    await triage.on_quality_chosen(
        callback, _FakeState({"apparat_name_text": "Аппарат №1"}), None,
        _SuppliesApi(pages=3, toner={"black": 4}),
    )

    assert len(seen["cards"]) == 1
    assert seen["created"][0]["problem_type"] == "print_quality"


async def test_at_night_the_quality_complaint_is_not_a_ticket(monkeypatch):
    # Nothing to read while the kiosks are off - say so and invite them back,
    # instead of filing a report nobody can act on.
    monkeypatch.setattr(apparats.tz, "now", lambda: datetime(2026, 9, 8, 23, 58))
    seen = {}
    _scripted(monkeypatch, seen)

    callback = _FakeCallback("quality:faded")
    await triage.on_quality_chosen(
        callback, _FakeState({"apparat_name_text": "Аппарат №4"}), None, _SuppliesApi(pages=283)
    )

    assert seen == {}
    assert any("8:00" in t for t in callback.message.edited)


async def test_at_night_a_supplies_report_is_not_a_ticket(monkeypatch):
    monkeypatch.setattr(apparats.tz, "now", lambda: datetime(2026, 9, 8, 23, 58))
    seen = {}
    _scripted(monkeypatch, seen)

    callback = _FakeCallback("problem:device_issue")
    await triage._report_device_issue(
        None, _SuppliesApi(pages=283), callback, _FakeState({}), "Аппарат №4"
    )

    assert seen == {}
    assert any("8:00" in t for t in callback.message.edited)
