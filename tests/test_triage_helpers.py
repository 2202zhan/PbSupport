from datetime import datetime, timedelta
from types import SimpleNamespace

import storage
import triage
from guard_rules import Decision


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


def test_quality_replies_no_longer_falsely_claim_staff_was_notified():
    # _send_scripted_reply never actually escalates - these used to claim
    # "Передал сотруднику" right away, which wasn't true.
    assert "Передал сотруднику" not in triage._QUALITY_REPLIES["faded"]
    assert "Передал сотруднику" not in triage._QUALITY_REPLIES["streaks"]


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
    await triage.on_nothelped_detail_provided(message, state=None, bot=None, api=None)
    assert message.answered_with == ["Пожалуйста, напишите текстом, что именно не так."]
