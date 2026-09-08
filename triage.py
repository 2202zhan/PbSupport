import asyncio
import logging
import re
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReactionTypeEmoji,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

import advice
import ai_decider
import concierge
import csat
import diagnosis
import notify
import receipt_parser
import sessions
import storage
import tz
from api_client import PrintBoxAPIClient, PrintBoxAPIError
from config import settings
from guard_rules import Decision, RefundReview, review_refund_case

logger = logging.getLogger(__name__)

router = Router(name="triage")
# Everything in this module talks to one user in their own chat. Without this,
# the catch-all at the bottom would also answer every staff message in the
# support group - and once privacy mode is off there, that means running the
# concierge model on staff chatter. Callback queries are unaffected.
router.message.filter(F.chat.type == "private")

_MAX_AI_ROUNDS = 3
# Deliberately paced (not just "as fast as the API responds") so the staged
# investigation message feels like real, thorough work rather than an instant
# reply - 3 pauses here plus the real gather_evidence/AI-decision time lands
# the whole sequence around 30-45s end to end.
_STAGE_PAUSE_SECONDS = 10

_PROBLEM_LABELS = {
    "upload_failed": "1️⃣ Файл не загрузился",
    "not_printed": "2️⃣ Не печатает документ",
    "payment_error": "3️⃣ Ошибка при оплате",
    "print_quality": "4️⃣ Плохое качество печати",
    "device_issue": "5️⃣ Бумага/тонер закончились",
    "other": "6️⃣ Другая проблема",
}

_DESCRIPTION_PROMPTS = {
    "other": "Опишите своими словами, что случилось — я разберусь.",
}

# Structured intake for the technical categories - buttons cover the common answers,
# with a "свой ответ" escape hatch for everything else, so free text is the
# exception rather than the default.
_NOT_PRINTED_SITUATION_OPTIONS = [
    ("paid", "💳 Оплатил(а), но распечатка не вышла"),
    ("other", "🔧 Другая ситуация (не про оплату)"),
]

_PAYMENT_ERROR_TYPE_OPTIONS = [
    ("paid_not_printed", "🖨 Оплатил(а), но не распечатал(а)"),
    ("qr_not_shown", "📵 QR-код не появился на экране"),
    ("bank_error", "🏦 Ошибка в приложении банка при оплате"),
    ("custom", "✏️ Другое"),
]

_UPLOAD_ISSUE_OPTIONS = [
    ("upload_error", "❌ Пришла ошибка при загрузке"),
    ("no_code", "🔢 Код не пришёл"),
    ("custom", "✏️ Другое"),
]

_QUALITY_ISSUE_OPTIONS = [
    ("faded", "🌫 Бледная печать"),
    ("streaks", "▬ Полосы/пятна"),
    ("missing_pages", "📄 Не все страницы"),
    ("custom", "✏️ Другое"),
]

# These sub-options have a known, predictable cause - no need for receipt/time
# questions or SNMP/log lookups, just answer directly. qr_not_shown is the one
# exception that needs a live check (see _check_qr_payment_availability) since
# the right answer depends on whether the apparat's payment path is up.
_BANK_ERROR_REPLY = (
    "🏦 Похоже, дело на стороне банка. Пожалуйста, обратитесь в поддержку вашего "
    "банковского приложения — мы не можем повлиять на ошибки на этом этапе оплаты."
)

_QR_OK_REPLY = (
    "📵 Проверил — аппарат сейчас в сети и работает исправно, похоже на разовый сбой "
    "экрана. Попробуйте, пожалуйста, ещё раз: выбрать настройки печати и оплатить заново."
)

_QR_BANK_SIDE_REPLY = (
    "📵 Проверил — сейчас оплата на этом аппарате недоступна. Это похоже на проблему "
    "на стороне платёжного сервиса, а не у вас. Попробуйте через несколько минут или "
    "на другом аппарате."
)

_UPLOAD_FORMAT_REPLY = (
    "📄 Проверьте формат вашего файла — мы принимаем: PDF, DOC, DOCX, PPTX, XLSX. "
    "Фото и скриншоты не принимаем."
)

_NO_CODE_REPLY = (
    "🔢 Попробуйте нажать «Получить код» в меню основного бота — код приходит сразу после этого."
)

_QUALITY_REPLIES = {
    # "faded" is answered from the apparat's real toner level instead - see
    # _faded_print_reply. The others have no live signal to check against.
    "streaks": (
        "▬ Полосы и пятна означают, что аппарату нужно обслуживание — это на нас. "
        "Передам сотрудникам, чтобы посмотрели. Если распечатка испорчена и вы платили "
        "за неё — нажмите «😕 Не помогло», разберёмся с возвратом."
    ),
    "missing_pages": (
        "📄 Если в документе были пустые (белые) страницы, иногда они не печатаются — "
        "проверьте, нет ли среди недостающих именно таких, и что в настройках было "
        "выбрано «Все страницы», а не диапазон."
    ),
}

# Best-effort heuristic: a top-level "Другая проблема" report that actually mentions
# payment/printing gets routed into the same when/receipt flow as "Не печатает",
# instead of being judged from free text alone with no evidence at all.
_PAYMENT_OR_PRINT_HINT_RE = re.compile(
    r"оплат|плати|деньг|списал|тенге|₸|чек|квитанц|печат|распечат|принтер|заказ", re.IGNORECASE
)


def _mentions_payment_or_print(text: str) -> bool:
    return bool(_PAYMENT_OR_PRINT_HINT_RE.search(text))

_WHEN_OPTIONS = [
    ("just_now", "🕐 Только что (до 5 мин)"),
    ("recent", "🕐 10–30 минут назад"),
    ("hours", "🕐 1–3 часа назад"),
    ("today", "🕐 Сегодня, раньше"),
    ("old", "📅 Вчера или раньше"),
    ("custom", "✏️ Указать точнее"),
]

_WHEN_TO_MINUTES_AGO = {
    "just_now": 2,
    "recent": 20,
    "hours": 120,
    "today": 300,
}

_AMOUNT_OPTIONS = [
    ("lt50", "до 50 ₸", 35, 50),
    ("50_150", "50–150 ₸", 100, 75),
    ("150_500", "150–500 ₸", 300, 175),
    ("gt500", "более 500 ₸", 600, 300),
    ("unknown", "🤷 Не помню", None, None),
    ("custom", "✏️ Указать точно", None, None),
]

_TOO_OLD_MESSAGE = (
    "😔 К сожалению, заявки старше 24 часов мы не можем проверить и оформить возврат через "
    "бота — технические данные за это время уже не сохраняются. Если ситуация всё ещё "
    "актуальна, опишите её сотруднику на месте."
)

# Best-effort: catches free-text "when" answers that clearly mean >24h ago, so the
# 24h cutoff isn't only reachable via the explicit button.
_OLD_WHEN_TEXT_RE = re.compile(r"вчера|позавчера|недел|месяц|\d+\s*дн", re.IGNORECASE)


def _custom_when_indicates_old(text: str) -> bool:
    return bool(_OLD_WHEN_TEXT_RE.search(text))


def _is_too_old(manual_hint_time) -> bool:
    """True once a resolved incident time (bucket, custom text, or a parsed
    receipt) is more than 24h in the past - the receipt is ground truth and can
    reveal this even when the user's own "when" answer suggested otherwise."""
    return manual_hint_time is not None and tz.now() - manual_hint_time > timedelta(hours=24)


_AMOUNT_TEXT_RE = re.compile(r"(\d+(?:[.,]\d+)?)")

_FREE_TEXT_LIMIT = 2000


def _clip_free_text(text: str) -> str:
    """Caps user-typed free text before it reaches ticket storage, AI prompts,
    or staff messages. Without this, a giant paste could (a) inflate AI token
    cost, or (b) push the staff escalation message past Telegram's 4096-char
    limit and make the send itself fail, losing the escalation entirely."""
    return text[:_FREE_TEXT_LIMIT]


def _parse_amount_text(text: str) -> float | None:
    """Best-effort number extraction from a free-typed exact amount (the
    "✏️ Указать точно" escape hatch) - without this, typed amounts only ever
    reached the human-readable summary and never actually helped match a
    transaction, which defeats the point of offering an exact-amount option."""
    match = _AMOUNT_TEXT_RE.search(text.replace(" ", ""))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


class TicketFlow(StatesGroup):
    choosing_apparat = State()
    naming_apparat = State()
    choosing_problem = State()
    intake_qa = State()
    intake_custom = State()
    awaiting_receipt_photo = State()
    awaiting_phone_for_escalation = State()
    awaiting_nothelped_detail = State()
    awaiting_escalation_receipt = State()
    describing = State()
    in_dialogue = State()


def _is_admin(telegram_id: str) -> bool:
    admin_ids = {x.strip() for x in settings.admin_telegram_ids.split(",") if x.strip()}
    return telegram_id in admin_ids


def _admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📸 Скриншот экрана (тест)", callback_data="admin:screenshot")],
        ]
    )


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="ℹ️ Как начать работу", callback_data="menu:faq")],
            [InlineKeyboardButton(text="📞 Сообщить о проблеме", callback_data="menu:report")],
            [InlineKeyboardButton(text="💬 Задать свой вопрос", callback_data="menu:ask")],
        ]
    )


def _faq_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 Как отправить файл?", callback_data="faq:send_file")],
            [InlineKeyboardButton(text="🔑 Как получить код?", callback_data="faq:get_code")],
            [InlineKeyboardButton(text="📄 Какие форматы поддерживаются?", callback_data="faq:formats")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="faq:back")],
        ]
    )


def _back_to_faq_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="faq:back")]]
    )


def _problem_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"problem:{key}")]
            for key, label in _PROBLEM_LABELS.items()
        ]
    )


def _feedback_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👍 Помогло", callback_data=f"feedback:helped:{ticket_id}"),
                InlineKeyboardButton(text="😕 Не помогло", callback_data=f"feedback:nothelped:{ticket_id}"),
            ]
        ]
    )


def _phone_request_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться номером", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


_PHONE_DIGITS_RE = re.compile(r"\d")


def _looks_like_phone_number(text: str) -> bool:
    return len(_PHONE_DIGITS_RE.findall(text)) >= 7


def _resolve_phone_input(contact_phone: str | None, text: str | None) -> str | None:
    """Phone is mandatory here (no skip option) - returns None if nothing
    recognizable as a phone number was given, so the caller re-asks instead of
    escalating with no way for staff to reach the user."""
    if contact_phone:
        return contact_phone
    if text and _looks_like_phone_number(text):
        return text.strip()
    return None


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отменить заявку", callback_data="cancel")]]
    )


def _with_cancel(keyboard: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[*keyboard.inline_keyboard, *_cancel_keyboard().inline_keyboard]
    )


def _not_printed_situation_keyboard() -> InlineKeyboardMarkup:
    return _with_cancel(
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=label, callback_data=f"situation:{key}")]
                for key, label in _NOT_PRINTED_SITUATION_OPTIONS
            ]
        )
    )


def _payment_error_type_keyboard() -> InlineKeyboardMarkup:
    return _with_cancel(
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=label, callback_data=f"payerr:{key}")]
                for key, label in _PAYMENT_ERROR_TYPE_OPTIONS
            ]
        )
    )


def _upload_issue_keyboard() -> InlineKeyboardMarkup:
    return _with_cancel(
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=label, callback_data=f"uploadissue:{key}")]
                for key, label in _UPLOAD_ISSUE_OPTIONS
            ]
        )
    )


def _quality_issue_keyboard() -> InlineKeyboardMarkup:
    return _with_cancel(
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=label, callback_data=f"quality:{key}")]
                for key, label in _QUALITY_ISSUE_OPTIONS
            ]
        )
    )


def _when_keyboard() -> InlineKeyboardMarkup:
    return _with_cancel(
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=label, callback_data=f"when:{key}")]
                for key, label in _WHEN_OPTIONS
            ]
        )
    )


def _receipt_keyboard() -> InlineKeyboardMarkup:
    return _with_cancel(
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📎 Отправлю чек об оплате", callback_data="receipt:photo")],
                [InlineKeyboardButton(text="🤷 Чека нет / не помню сумму", callback_data="receipt:skip")],
            ]
        )
    )


def _amount_keyboard() -> InlineKeyboardMarkup:
    return _with_cancel(
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=label, callback_data=f"amount:{key}")]
                for key, label, _, _ in _AMOUNT_OPTIONS
            ]
        )
    )


def _no_match_receipt_keyboard() -> InlineKeyboardMarkup:
    return _with_cancel(
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📎 Отправить чек оплаты", callback_data="nomatch_receipt:photo")],
                [InlineKeyboardButton(text="🤷 Нет чека", callback_data="nomatch_receipt:skip")],
            ]
        )
    )


def _escalation_receipt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📎 Прислать чек", callback_data="escreceipt:photo")],
            [InlineKeyboardButton(text="🤷 Нет чека", callback_data="escreceipt:skip")],
        ]
    )


async def _apparat_keyboard(api: PrintBoxAPIClient) -> InlineKeyboardMarkup:
    """A failure here must still leave the user a way forward: they can always
    type the apparat name, and the ticket carries that text either way."""
    try:
        apparats = await api.get_apparats()
    except PrintBoxAPIError:
        logger.exception("could not list apparats")
        apparats = []
    rows = [
        [InlineKeyboardButton(text=a.name_apparat, callback_data=f"apparat:{a.name_apparat}")]
        for a in apparats
    ]
    label = "Не вижу свой аппарат" if apparats else "Указать аппарат вручную"
    rows.append([InlineKeyboardButton(text=label, callback_data="apparat:__other__")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    sessions.forget(str(message.from_user.id))
    text = (
        "👋 Привет! Я — умный ИИ-ассистент поддержки PrintBox 🤖✨\n\n"
        "Отвечу на любой вопрос сразу и сам разберусь, если что-то пошло не так с печатью, "
        "оплатой или файлом — без ожидания живого оператора. А если случай сложный — передам "
        "команде и прослежу, чтобы вам ответили.\n\n"
        "Выберите, с чего начать 👇"
    )
    if _is_admin(str(message.from_user.id)):
        text += "\n\n🔧 Вы админ — команда /admin откроет админ-панель."
    await message.answer(text, reply_markup=_main_menu_keyboard())


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    if not _is_admin(str(message.from_user.id)):
        await message.answer("Эта команда недоступна.")
        return
    await state.clear()
    await message.answer("🔧 Админ-панель", reply_markup=_admin_menu_keyboard())


@router.callback_query(F.data == "admin:screenshot")
async def on_admin_screenshot(callback: CallbackQuery) -> None:
    if not _is_admin(str(callback.from_user.id)):
        await callback.answer("Недоступно", show_alert=True)
        return
    await callback.message.edit_text(
        "📸 Скриншот экрана аппарата сейчас недоступен — в API PrintBox нет эндпоинта для "
        "получения изображения с экрана (есть только lock_screen — блокировка/разблокировка "
        "экрана, но не захват картинки).\n\n"
        "Чтобы это заработало, нужен новый эндпоинт на стороне backend — по аналогии с тем, "
        "как сейчас собираются логи: ПК аппарата делает скриншот и отправляет его тем же "
        "путём (через WebSocket-запрос вроде request-logs), а админка отдаёт готовое изображение.",
        reply_markup=_admin_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:faq")
async def on_menu_faq(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Чем подсказать?", reply_markup=_faq_keyboard())
    sessions.touch(str(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "faq:back")
@router.callback_query(F.data == "menu:back")
async def on_back_to_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        "👋 Выберите, с чего начать 👇", reply_markup=_main_menu_keyboard()
    )
    sessions.forget(str(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data.startswith("faq:"))
async def on_faq_answer(callback: CallbackQuery) -> None:
    key = callback.data.split(":", 1)[1]
    text = advice.FAQ_ANSWERS.get(key, "Не нашёл ответ, уточните вопрос, пожалуйста.")
    await callback.message.edit_text(text, reply_markup=_back_to_faq_keyboard())
    sessions.touch(str(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "menu:ask")
async def on_menu_ask(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        "Напишите ваш вопрос — отвечу сразу 🙂",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:back")]]
        ),
    )
    sessions.forget(str(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "menu:report")
async def on_menu_report(callback: CallbackQuery, state: FSMContext, api: PrintBoxAPIClient) -> None:
    await state.set_state(TicketFlow.choosing_apparat)
    await callback.message.edit_text(
        "На каком аппарате это случилось?", reply_markup=await _apparat_keyboard(api)
    )
    sessions.touch(str(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data.startswith("apparat:"), TicketFlow.choosing_apparat)
async def on_apparat_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    apparat_text = callback.data.split(":", 1)[1]
    if apparat_text == "__other__":
        await callback.message.edit_text("Напишите, пожалуйста, где находится аппарат (адрес/этаж).")
        await state.set_state(TicketFlow.naming_apparat)
    else:
        await state.update_data(apparat_name_text=apparat_text)
        await state.set_state(TicketFlow.choosing_problem)
        await callback.message.edit_text(
            f"Аппарат: {apparat_text}\n\nВыберите, пожалуйста, проблему:",
            reply_markup=_problem_keyboard(),
        )
    sessions.touch(str(callback.from_user.id))
    await callback.answer()


@router.message(TicketFlow.naming_apparat)
async def on_apparat_named(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("Пожалуйста, напишите название/место аппарата текстом.")
        sessions.touch(str(message.from_user.id))
        return
    await state.update_data(apparat_name_text=_clip_free_text(message.text))
    await state.set_state(TicketFlow.choosing_problem)
    await message.answer("Выберите, пожалуйста, проблему:", reply_markup=_problem_keyboard())
    sessions.touch(str(message.from_user.id))


@router.callback_query(F.data.startswith("problem:"), TicketFlow.choosing_problem)
async def on_problem_chosen(callback: CallbackQuery, state: FSMContext, bot: Bot, api: PrintBoxAPIClient) -> None:
    problem_type = callback.data.split(":", 1)[1]
    await state.update_data(problem_type=problem_type, dialogue_history=[], rounds=0, intake={})

    if problem_type == "not_printed":
        await state.set_state(TicketFlow.intake_qa)
        await callback.message.edit_text("Уточните ситуацию:", reply_markup=_not_printed_situation_keyboard())
    elif problem_type == "payment_error":
        await state.set_state(TicketFlow.intake_qa)
        await callback.message.edit_text(
            "Что именно произошло при оплате?", reply_markup=_payment_error_type_keyboard()
        )
    elif problem_type == "print_quality":
        await state.set_state(TicketFlow.intake_qa)
        await callback.message.edit_text(
            "Что не так с распечаткой?", reply_markup=_quality_issue_keyboard()
        )
    elif problem_type == "upload_failed":
        await state.set_state(TicketFlow.intake_qa)
        await callback.message.edit_text(
            "Что произошло с файлом?", reply_markup=_upload_issue_keyboard()
        )
    elif problem_type == "device_issue":
        # No payment/print to diagnose - just a heads-up about the apparat
        # itself, relay it straight to staff with no diagnosis pipeline.
        data = await state.get_data()
        await _report_device_issue(bot, api, callback, state, data.get("apparat_name_text") or "")
        return
    else:
        # Only reachable for the top-level "6️⃣ Другая проблема" - mark it so
        # on_description knows this free text was never given any other
        # category context, and can reclassify it if it sounds payment/print related.
        await _update_intake(state, is_top_level_other=True)
        await state.set_state(TicketFlow.describing)
        await callback.message.edit_text(_DESCRIPTION_PROMPTS[problem_type], reply_markup=_cancel_keyboard())
    sessions.touch(str(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data.startswith("situation:"), TicketFlow.intake_qa)
async def on_not_printed_situation_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 1)[1]
    if key == "other":
        # Not actually about a payment gone wrong - there's likely no transaction to
        # look up, so skip the when/receipt/amount dance and just let them describe it.
        await state.update_data(problem_type="other")
        await state.set_state(TicketFlow.describing)
        await callback.message.edit_text(
            "Расскажите, что произошло — на каком шаге застряли?", reply_markup=_cancel_keyboard()
        )
    else:
        await _update_intake(state, situation="Оплатил(а), но распечатка не вышла")
        await callback.message.edit_text("Когда это случилось?", reply_markup=_when_keyboard())
    sessions.touch(str(callback.from_user.id))
    await callback.answer()


async def _send_scripted_reply(
    state: FSMContext,
    message: Message,
    telegram_id: str,
    username: str | None,
    problem_type: str,
    apparat_name_text: str,
    raw_text: str,
    reply_text: str,
    payment_expected: bool = True,
) -> int:
    """For sub-options where the cause is already known from the button alone (or
    a quick live check) - no transaction could exist or there's nothing useful to
    look up, so skip the full diagnosis pipeline and just answer directly.

    Returns the ticket id, so a caller that also wants to notify staff (see the
    low-toner branch of on_quality_chosen) can escalate the same ticket."""
    ticket_id = await storage.create_ticket(
        telegram_id=telegram_id,
        username=username,
        contact=None,
        problem_type=problem_type,
        apparat_name=apparat_name_text,
        raw_text=raw_text,
        payment_expected=payment_expected,
    )
    await storage.record_decision(ticket_id, {}, None, None, None, "give_advice")
    await message.answer(reply_text, reply_markup=_feedback_keyboard(ticket_id))
    sessions.forget(telegram_id)
    await state.clear()
    return ticket_id


async def _suggest_alternate_apparat(api: PrintBoxAPIClient, apparat_name_text: str) -> str | None:
    """Looks for another currently-online apparat with no active error, to
    mention as a stopgap while this one's paper/toner gets refilled. This is a
    nice-to-have on top of the actual report - an API hiccup here must not
    leave the user without any confirmation at all."""
    try:
        reported = await diagnosis.find_apparat_by_name(api, apparat_name_text)
        apparats = await api.get_apparats()
        statuses = {s.get("apparat_id"): s for s in await api.get_all_printer_statuses()}
    except PrintBoxAPIError:
        logger.warning("could not look up an alternate apparat for %s", apparat_name_text)
        return None
    for a in apparats:
        if reported is not None and a.id == reported.id:
            continue
        status = statuses.get(a.id)
        if status and status.get("is_online") and not status.get("error_text"):
            return f"{a.name_apparat} ({a.address})" if a.address else a.name_apparat
    return None


_LOW_PAGES = 50
_LOW_TONER = 20


async def _read_supplies(api: PrintBoxAPIClient, apparat_name_text: str) -> tuple[str, str]:
    """Looks up what the machine itself reports about paper and toner, so a
    "закончилась бумага" report doesn't reach staff as bare hearsay. Returns
    (what to tell the user, what to tell staff)."""
    try:
        apparat = await diagnosis.find_apparat_by_name(api, apparat_name_text)
        statuses = await api.get_all_printer_statuses()
    except PrintBoxAPIError:
        logger.warning("could not read supplies for %s", apparat_name_text)
        return "", "показания аппарата сейчас недоступны"
    if apparat is None:
        return "", "аппарат не найден в справочнике"

    current = next((s for s in statuses if s.get("apparat_id") == apparat.id), None) or {}
    toner = {k: v for k, v in (current.get("toner") or {}).items() if isinstance(v, (int, float))}
    lowest_toner = min(toner.values()) if toner else None
    pages = apparat.pages_left
    error_text = current.get("error_text")

    findings = []
    if pages is not None:
        findings.append(f"{pages} {_sheets_word(pages)} бумаги")
    if lowest_toner is not None:
        findings.append(f"тонер {lowest_toner}%")
    if error_text:
        findings.append(f"аппарат сообщает: {error_text}")
    staff_note = ", ".join(findings) if findings else "показаний от аппарата нет"

    confirms = bool(error_text) or (pages is not None and pages < _LOW_PAGES) or (
        lowest_toner is not None and lowest_toner < _LOW_TONER
    )
    if confirms:
        user_note = f" Проверил — {staff_note}. Похоже, так и есть."
    elif findings:
        user_note = (
            f" Проверил — по нашим счётчикам осталось {staff_note}. Счётчик может расходиться "
            "с тем, что в лотке (например, замялся лист), поэтому передаю сотруднику."
        )
    else:
        user_note = ""
    return user_note, staff_note


def _sheets_word(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return "листов"
    return {1: "лист", 2: "листа", 3: "листа", 4: "листа"}.get(n % 10, "листов")


async def _report_device_issue(
    bot: Bot, api: PrintBoxAPIClient, callback: CallbackQuery, state: FSMContext, apparat_name_text: str
) -> None:
    """No payment/print to diagnose for "бумага/тонер закончились" - but the
    machine does report its own paper and toner, so check that first and hand
    staff the readings instead of a bare "юзер сообщает"."""
    telegram_id = str(callback.from_user.id)
    username = callback.from_user.username
    await callback.message.edit_text("🔍 Проверяю состояние аппарата...")
    user_note, staff_note = await _read_supplies(api, apparat_name_text)

    ticket_id = await storage.create_ticket(
        telegram_id=telegram_id,
        username=username,
        contact=None,
        problem_type="device_issue",
        apparat_name=apparat_name_text,
        raw_text="Юзер сообщает: бумага/тонер закончились",
        payment_expected=False,
    )
    await _escalate(
        bot, api, callback.message, ticket_id, None,
        Decision(
            action="escalate",
            reason="device_issue_reported",
            staff_summary=f"📋 Юзер сообщает: на аппарате «{apparat_name_text}» закончилась "
            f"бумага или тонер.\nПоказания аппарата: {staff_note}.",
        ),
    )
    alternate = await _suggest_alternate_apparat(api, apparat_name_text)
    note = f" Пока можно воспользоваться аппаратом «{alternate}», если рядом." if alternate else ""
    await callback.message.edit_text(
        f"Спасибо!{user_note} Передал сотруднику — на аппарате «{apparat_name_text}» "
        f"проверят бумагу и тонер.{note}",
        reply_markup=_main_menu_keyboard(),
    )
    sessions.forget(telegram_id)
    await state.clear()


async def _send_concierge_reply(
    state: FSMContext,
    message: Message,
    telegram_id: str,
    username: str | None,
    problem_type: str,
    apparat_name_text: str,
    raw_text: str,
) -> None:
    reply_text = await concierge.answer(raw_text)
    await _send_scripted_reply(
        state, message, telegram_id, username, problem_type, apparat_name_text, raw_text, reply_text
    )


async def _check_qr_payment_availability(api: PrintBoxAPIClient, apparat_name_text: str) -> bool:
    """Best-effort proxy for "is payment/QR generation currently working on this
    apparat" - there's no direct Kaspi-status endpoint, so the apparat's
    online/error status from our own monitoring is the closest available signal."""
    apparat = await diagnosis.find_apparat_by_name(api, apparat_name_text)
    if apparat is None:
        return False
    statuses = await api.get_all_printer_statuses()
    current = next((s for s in statuses if s.get("apparat_id") == apparat.id), None)
    if current is None:
        return False
    return bool(current.get("is_online")) and not current.get("error_text")


@router.callback_query(F.data.startswith("payerr:"), TicketFlow.intake_qa)
async def on_payment_error_type_chosen(callback: CallbackQuery, state: FSMContext, api: PrintBoxAPIClient) -> None:
    key = callback.data.split(":", 1)[1]
    data = await state.get_data()
    apparat_name_text = data.get("apparat_name_text") or ""
    telegram_id = str(callback.from_user.id)
    username = callback.from_user.username

    if key == "custom":
        await state.update_data(pending_question="payerr")
        await state.set_state(TicketFlow.intake_custom)
        await callback.message.edit_text(
            "Опишите своими словами, что произошло при оплате.", reply_markup=_cancel_keyboard()
        )
        sessions.touch(telegram_id)
        await callback.answer()
        return

    if key == "qr_not_shown":
        await callback.answer()
        await callback.message.edit_text("🔍 Проверяю доступность оплаты на аппарате...")
        is_available = await _check_qr_payment_availability(api, apparat_name_text)
        reply_text = _QR_OK_REPLY if is_available else _QR_BANK_SIDE_REPLY
        # No QR means no payment could have gone through - never ask this user
        # for a receipt later.
        await _send_scripted_reply(
            state, callback.message, telegram_id, username, "payment_error", apparat_name_text,
            "QR-код не появился на экране", reply_text, payment_expected=False,
        )
        return

    if key == "bank_error":
        await _send_scripted_reply(
            state, callback.message, telegram_id, username, "payment_error", apparat_name_text,
            "Ошибка в приложении банка при оплате", _BANK_ERROR_REPLY, payment_expected=False,
        )
        await callback.answer()
        return

    # paid_not_printed - the one sub-option where money may genuinely be stuck,
    # so it's the only one that still warrants the full when/receipt/SNMP check.
    label = dict(_PAYMENT_ERROR_TYPE_OPTIONS)[key]
    await _update_intake(state, payment_issue=label)
    await callback.message.edit_text("Когда это случилось?", reply_markup=_when_keyboard())
    sessions.touch(telegram_id)
    await callback.answer()


@router.callback_query(F.data.startswith("uploadissue:"), TicketFlow.intake_qa)
async def on_upload_issue_chosen(callback: CallbackQuery, state: FSMContext, bot: Bot, api: PrintBoxAPIClient) -> None:
    key = callback.data.split(":", 1)[1]
    data = await state.get_data()
    apparat_name_text = data.get("apparat_name_text") or ""
    telegram_id = str(callback.from_user.id)
    username = callback.from_user.username

    if key == "custom":
        await state.update_data(pending_question="upload_issue")
        await state.set_state(TicketFlow.intake_custom)
        await callback.message.edit_text(
            "Опишите своими словами, что произошло с файлом.", reply_markup=_cancel_keyboard()
        )
        sessions.touch(telegram_id)
        await callback.answer()
        return

    label = dict(_UPLOAD_ISSUE_OPTIONS)[key]
    reply_text = _UPLOAD_FORMAT_REPLY if key == "upload_error" else _NO_CODE_REPLY
    # Uploading happens before payment, so there is no receipt to ask for.
    await _send_scripted_reply(
        state, callback.message, telegram_id, username, "upload_failed", apparat_name_text,
        label, reply_text, payment_expected=False,
    )
    await callback.answer()


async def _faded_print_reply(api: PrintBoxAPIClient, apparat_name_text: str) -> tuple[str, bool]:
    """Answers a "faded print" complaint from the apparat's real toner level.
    The user can't act on toner either way - it's ours to refill - so the reply
    only ever states what we found and what we're doing about it.

    Returns (reply, tell_staff): a confirmed low cartridge is worth waking staff
    for on its own, the same as a "бумага/тонер закончились" report."""
    level = None
    try:
        apparat = await diagnosis.find_apparat_by_name(api, apparat_name_text)
        if apparat is not None:
            statuses = await api.get_all_printer_statuses()
            current = next((s for s in statuses if s.get("apparat_id") == apparat.id), None)
            toner = (current or {}).get("toner") or {}
            if toner:
                level = min(v for v in toner.values() if isinstance(v, (int, float)))
    except PrintBoxAPIError:
        logger.warning("could not read toner for %s", apparat_name_text)

    if level is not None and level < 20:
        # Only this branch actually reaches staff (see on_quality_chosen), so
        # only this branch is allowed to say so.
        return (
            f"🌫 Проверил аппарат — тонер действительно на исходе ({level}%). Это на нашей "
            "стороне, передаю сотрудникам, чтобы заменили. Пока можно распечатать на другом "
            "аппарате. Если распечатка испорчена и вы за неё платили — нажмите «😕 Не помогло»."
        ), True
    if level is not None:
        return (
            f"🌫 Проверил аппарат — тонера достаточно ({level}%), так что дело, скорее всего, "
            "в чём-то другом. Если распечатка испорчена и вы за неё платили — нажмите "
            "«😕 Не помогло», передам сотруднику и разберёмся с возвратом."
        ), False
    return (
        "🌫 Сейчас не могу проверить состояние аппарата. Если распечатка испорчена и вы за "
        "неё платили — нажмите «😕 Не помогло», передам сотруднику."
    ), False


@router.callback_query(F.data.startswith("quality:"), TicketFlow.intake_qa)
async def on_quality_chosen(
    callback: CallbackQuery, state: FSMContext, bot: Bot, api: PrintBoxAPIClient
) -> None:
    key = callback.data.split(":", 1)[1]
    data = await state.get_data()
    apparat_name_text = data.get("apparat_name_text") or ""
    telegram_id = str(callback.from_user.id)
    username = callback.from_user.username

    if key == "custom":
        await state.update_data(pending_question="quality")
        await state.set_state(TicketFlow.intake_custom)
        await callback.message.edit_text(
            "Что именно не так с распечаткой? Опишите своими словами.", reply_markup=_cancel_keyboard()
        )
        sessions.touch(telegram_id)
        await callback.answer()
        return

    label = dict(_QUALITY_ISSUE_OPTIONS)[key]
    tell_staff = False
    if key == "faded":
        reply, tell_staff = await _faded_print_reply(api, apparat_name_text)
    else:
        reply = _QUALITY_REPLIES[key]

    ticket_id = await _send_scripted_reply(
        state, callback.message, telegram_id, username, "print_quality", apparat_name_text,
        label, reply,
    )
    if tell_staff:
        await _escalate(
            bot, api, callback.message, ticket_id, None,
            Decision(
                action="escalate",
                reason="low_toner_reported",
                staff_summary=f"🌫 Юзер жалуется на бледную печать на «{apparat_name_text}», "
                "тонер по данным мониторинга на исходе — нужно заменить картридж.",
            ),
        )
    sessions.touch(telegram_id)
    await callback.answer()


@router.callback_query(F.data.startswith("when:"), TicketFlow.intake_qa)
async def on_when_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 1)[1]
    if key == "custom":
        await state.update_data(pending_question="when")
        await state.set_state(TicketFlow.intake_custom)
        await callback.message.edit_text(
            "Когда это было? Напишите дату/время, как помните.", reply_markup=_cancel_keyboard()
        )
    elif key == "old":
        await _close_as_too_old(
            callback.message, state, str(callback.from_user.id), callback.from_user.username, "вчера или раньше"
        )
    else:
        label = dict(_WHEN_OPTIONS)[key]
        await _update_intake(state, when_label=label, when_minutes_ago=_WHEN_TO_MINUTES_AGO[key])
        await callback.message.edit_text(
            "Есть скрин/фото чека оплаты? Поможет точно найти заказ.", reply_markup=_receipt_keyboard()
        )
    sessions.touch(str(callback.from_user.id))
    await callback.answer()


async def _close_as_too_old(
    message: Message, state: FSMContext, telegram_id: str, username: str | None, when_text: str
) -> None:
    data = await state.get_data()
    ticket_id = await storage.create_ticket(
        telegram_id=telegram_id,
        username=username,
        contact=None,
        problem_type=data.get("problem_type", "other"),
        apparat_name=data.get("apparat_name_text") or "",
        raw_text=f"Слишком старая заявка ({when_text})",
    )
    await storage.set_ticket_status(ticket_id, "too_old")
    await message.answer(_TOO_OLD_MESSAGE, reply_markup=_main_menu_keyboard())
    sessions.forget(telegram_id)
    await state.clear()


@router.callback_query(F.data == "receipt:photo", TicketFlow.intake_qa)
async def on_receipt_photo_requested(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TicketFlow.awaiting_receipt_photo)
    await callback.message.edit_text(
        "Пришлите, пожалуйста, чек оплаты — фото или PDF.", reply_markup=_cancel_keyboard()
    )
    sessions.touch(str(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "receipt:skip", TicketFlow.intake_qa)
async def on_receipt_skipped(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("На какую сумму примерно вы платили?", reply_markup=_amount_keyboard())
    sessions.touch(str(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "nomatch_receipt:photo", TicketFlow.awaiting_receipt_photo)
async def on_no_match_receipt_photo_requested(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "Пришлите, пожалуйста, чек оплаты — фото или PDF.", reply_markup=_cancel_keyboard()
    )
    sessions.touch(str(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "nomatch_receipt:skip", TicketFlow.awaiting_receipt_photo)
async def on_no_match_receipt_skipped(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    if ticket_id:
        await storage.set_ticket_status(ticket_id, "no_order_found")
    await callback.message.edit_text(
        "Понял. В системе нет данных о вашем заказе за это время — без чека, к сожалению, "
        "нечего проверять. Если вы уверены, что платили, попробуйте уточнить время/сумму точнее "
        "или напишите нам другим способом.",
        reply_markup=_main_menu_keyboard(),
    )
    sessions.forget(str(callback.from_user.id))
    await state.clear()
    await callback.answer()


async def _extract_receipt_data(
    bot: Bot, message: Message
) -> tuple[str, bool, receipt_parser.ReceiptData | None]:
    """Returns (file_id, is_document, parsed). Only PDFs get parsed - photos are
    accepted (for a human to look at later) but there's no OCR to read them here."""
    if message.document:
        file_id = message.document.file_id
        is_pdf = message.document.mime_type == "application/pdf" or (
            message.document.file_name or ""
        ).lower().endswith(".pdf")
        if not is_pdf:
            return file_id, True, None
        try:
            buf = await bot.download(file_id)
            parsed = receipt_parser.parse_receipt_pdf(buf.read())
        except Exception:
            logger.exception("failed to download/parse receipt PDF")
            return file_id, True, None
        return file_id, True, (parsed if parsed.is_useful else None)
    if message.photo:
        return message.photo[-1].file_id, False, None
    return "", False, None


async def _forward_receipt_to_staff(bot: Bot, ticket_id: int, file_id: str, is_document: bool) -> None:
    await notify.forward_receipt(bot, ticket_id, file_id, is_document)


@router.message(TicketFlow.awaiting_receipt_photo, F.photo | F.document)
async def on_receipt_received(message: Message, state: FSMContext, bot: Bot, api: PrintBoxAPIClient) -> None:
    file_id, is_document, parsed = await _extract_receipt_data(bot, message)
    if not file_id:
        await message.answer("Не получилось прочитать файл, пришлите, пожалуйста, ещё раз.")
        return

    data = await state.get_data()
    if data.get("awaiting_post_diagnosis_receipt"):
        ticket_id = data["ticket_id"]
        if parsed is not None:
            # Got exact data from the receipt - worth a real second look instead of
            # jumping straight to "let a human sort it out".
            await message.answer("Чек получен и обработан, проверяю заново... 🔍")
            ticket_record = await storage.get_ticket(ticket_id)
            ticket_input = diagnosis.TicketInput(
                problem_type=ticket_record.problem_type,
                apparat_name_text=ticket_record.apparat_name or "",
                telegram_id=ticket_record.telegram_id,
                username=ticket_record.username,
                contact=ticket_record.contact,
                raw_text=ticket_record.raw_text or "",
                submitted_at=tz.now(),
                dialogue_history=data.get("dialogue_history", []),
                manual_hint_amount=parsed.amount,
                manual_hint_amount_tolerance=1.0,
                manual_hint_time=parsed.paid_at,
                manual_hint_time_tolerance_seconds=120,
                manual_hint_is_precise=True,
                receipt_photo_file_id=file_id,
                receipt_is_document=is_document,
            )
            await state.update_data(awaiting_post_diagnosis_receipt=False)
            await _run_decision_cycle(bot, api, state, message, ticket_id, ticket_input)
            return

        await message.answer(
            "Чек получен, спасибо! Передаю сотруднику для проверки.", reply_markup=_main_menu_keyboard()
        )
        await _escalate(
            bot, api, message, ticket_id, None,
            Decision(action="escalate", reason="no_matching_order_with_receipt_followup",
                     staff_summary=f"Заявка #{ticket_id}: не нашли заказ юзера в системе, юзер "
                     "прислал чек после запроса - нужна ручная проверка."),
        )
        await _forward_receipt_to_staff(bot, ticket_id, file_id, is_document)
        sessions.forget(str(message.from_user.id))
        await state.clear()
        return

    # Original intake step.
    if parsed is not None:
        await _update_intake(
            state,
            receipt_photo_file_id=file_id,
            receipt_is_document=is_document,
            receipt_amount=parsed.amount,
            receipt_paid_at=parsed.paid_at,
        )
        amount_text = f"{parsed.amount:.0f} ₸" if parsed.amount else "сумма не распознана"
        when_text = f", оплачено {parsed.paid_at:%d.%m %H:%M}" if parsed.paid_at else ""
        await message.answer(f"Чек обработан ✅ {amount_text}{when_text}.")
    else:
        await _update_intake(state, receipt_photo_file_id=file_id, receipt_is_document=is_document)
        await message.answer("Чек получен, спасибо! 📎")
    await _finalize_intake(message, str(message.from_user.id), message.from_user.username, state, bot, api)


@router.message(TicketFlow.awaiting_receipt_photo)
async def on_receipt_expected_but_text_sent(message: Message) -> None:
    await message.answer(
        "Пришлите, пожалуйста, именно чек оплаты — фото или PDF (или нажмите «❌ Отменить заявку» выше).",
    )
    sessions.touch(str(message.from_user.id))


@router.callback_query(F.data.startswith("amount:"), TicketFlow.intake_qa)
async def on_amount_chosen(callback: CallbackQuery, state: FSMContext, bot: Bot, api: PrintBoxAPIClient) -> None:
    key = callback.data.split(":", 1)[1]
    if key == "custom":
        await state.update_data(pending_question="amount")
        await state.set_state(TicketFlow.intake_custom)
        await callback.message.edit_text("Напишите точную сумму, на которую платили.", reply_markup=_cancel_keyboard())
        await callback.answer()
        return

    _, label, value, tolerance = next(o for o in _AMOUNT_OPTIONS if o[0] == key)
    if value is not None:
        await _update_intake(state, amount_label=label, amount_value=value, amount_tolerance=tolerance)
    await callback.answer()
    await _finalize_intake(
        callback.message, str(callback.from_user.id), callback.from_user.username, state, bot, api
    )


@router.message(TicketFlow.intake_custom)
async def on_intake_custom_answer(message: Message, state: FSMContext, bot: Bot, api: PrintBoxAPIClient) -> None:
    if not message.text:
        # Voice/sticker/video/etc instead of typed text - every branch below
        # assumes a real string (regex/parsing on None crashes the handler).
        await message.answer("Пожалуйста, напишите ответ текстом.")
        sessions.touch(str(message.from_user.id))
        return

    data = await state.get_data()
    pending = data.get("pending_question")
    apparat_name_text = data.get("apparat_name_text") or ""
    telegram_id = str(message.from_user.id)
    username = message.from_user.username
    text = _clip_free_text(message.text)

    if pending == "quality":
        await _send_concierge_reply(state, message, telegram_id, username, "print_quality", apparat_name_text, text)
        return
    elif pending == "payerr":
        await _send_concierge_reply(state, message, telegram_id, username, "payment_error", apparat_name_text, text)
        return
    elif pending == "upload_issue":
        await _update_intake(state, upload_issue=text)
        await _finalize_intake(message, str(message.from_user.id), message.from_user.username, state, bot, api)
        return
    elif pending == "when":
        if _custom_when_indicates_old(text):
            await _close_as_too_old(
                message, state, str(message.from_user.id), message.from_user.username, text
            )
            return
        await _update_intake(state, when_custom=text)
        await message.answer(
            "Есть скрин/фото чека оплаты? Поможет точно найти заказ.", reply_markup=_receipt_keyboard()
        )
    elif pending == "amount":
        parsed_amount = _parse_amount_text(text)
        fields = {"amount_custom": text}
        if parsed_amount is not None:
            fields["amount_value"] = parsed_amount
            fields["amount_tolerance"] = 5.0
        await _update_intake(state, **fields)
        await _finalize_intake(message, str(message.from_user.id), message.from_user.username, state, bot, api)
        return
    else:
        await message.answer("Пожалуйста, выберите вариант кнопкой выше 👆")
        return

    await state.set_state(TicketFlow.intake_qa)
    sessions.touch(str(message.from_user.id))


async def _update_intake(state: FSMContext, **fields) -> None:
    data = await state.get_data()
    intake = data.get("intake", {})
    intake.update(fields)
    await state.update_data(intake=intake)


def _build_intake_summary(problem_type: str, intake: dict) -> str:
    parts = [_PROBLEM_LABELS[problem_type].split(" ", 1)[1]]
    if intake.get("situation"):
        parts.append(intake["situation"])
    if intake.get("payment_issue"):
        parts.append(f"Тип ошибки оплаты: {intake['payment_issue']}")
    if intake.get("upload_issue"):
        parts.append(f"Проблема с файлом: {intake['upload_issue']}")
    if intake.get("quality_issue"):
        parts.append(f"Проблема: {intake['quality_issue']}")
    when = intake.get("when_custom") or intake.get("when_label")
    if when:
        parts.append(f"Когда: {when}")
    if intake.get("receipt_amount") is not None:
        paid_at = intake.get("receipt_paid_at")
        parts.append(
            f"Чек распознан: {intake['receipt_amount']:.0f} ₸"
            + (f" в {paid_at:%d.%m.%Y %H:%M:%S}" if paid_at else "")
        )
    elif intake.get("receipt_photo_file_id"):
        parts.append("Прислал(а) чек (без автоматического распознавания).")
    else:
        amount = intake.get("amount_custom") or intake.get("amount_label")
        if amount:
            parts.append(f"Сумма: {amount}")
    return ". ".join(parts)


async def _finalize_intake(
    message: Message, telegram_id: str, username: str | None, state: FSMContext, bot: Bot, api: PrintBoxAPIClient
) -> None:
    """`message` is only used as a target to send replies to (its chat) - the actual
    user identity must be passed explicitly, since when this is reached from a
    callback handler, `callback.message.from_user` would resolve to the bot itself,
    not the user who tapped the button."""
    data = await state.get_data()
    intake = data.get("intake", {})
    problem_type = data["problem_type"]
    apparat_name_text = data.get("apparat_name_text") or ""

    # A successfully parsed receipt gives an exact figure - prefer that over the
    # rough bucket the user picked/typed.
    manual_hint_time = intake.get("receipt_paid_at")
    manual_hint_time_tolerance = 120.0
    if manual_hint_time is None and intake.get("when_minutes_ago") is not None:
        manual_hint_time = tz.now() - timedelta(minutes=intake["when_minutes_ago"])
        manual_hint_time_tolerance = max(900, intake["when_minutes_ago"] * 60 * 0.5)

    # The receipt is ground truth and can reveal the payment is actually >24h
    # old even if the user picked a recent "when" bucket (or we skipped that
    # question via reclassification) - catch it here too, not just at the
    # when-button step, since this is the last point before we'd run a full
    # (pointless) investigation on an un-actionable ticket.
    if _is_too_old(manual_hint_time):
        await _close_as_too_old(
            message, state, telegram_id, username, manual_hint_time.strftime("%d.%m.%Y %H:%M")
        )
        return

    manual_hint_amount = intake.get("receipt_amount", intake.get("amount_value"))
    manual_hint_amount_tolerance = 1.0 if intake.get("receipt_amount") is not None else intake.get("amount_tolerance", 1.0)
    # Only an actual parsed receipt counts as precise enough to search *other
    # users'* transactions with - a self-reported bucket guess never does,
    # even if it happens to come with a narrow-looking tolerance.
    manual_hint_is_precise = intake.get("receipt_amount") is not None or intake.get("receipt_paid_at") is not None

    raw_text = _build_intake_summary(problem_type, intake)
    ticket_input = diagnosis.TicketInput(
        problem_type=problem_type,
        apparat_name_text=apparat_name_text,
        telegram_id=telegram_id,
        username=username,
        contact=None,
        raw_text=raw_text,
        submitted_at=tz.now(),
        dialogue_history=[f"user: {raw_text}"],
        manual_hint_amount=manual_hint_amount,
        manual_hint_amount_tolerance=manual_hint_amount_tolerance,
        manual_hint_time=manual_hint_time,
        manual_hint_time_tolerance_seconds=manual_hint_time_tolerance,
        manual_hint_is_precise=manual_hint_is_precise,
        receipt_photo_file_id=intake.get("receipt_photo_file_id"),
        receipt_is_document=intake.get("receipt_is_document", False),
    )
    ticket_id = await storage.create_ticket(
        telegram_id=ticket_input.telegram_id,
        username=ticket_input.username,
        contact=ticket_input.contact,
        problem_type=problem_type,
        apparat_name=apparat_name_text,
        raw_text=ticket_input.raw_text,
    )
    await state.update_data(ticket_id=ticket_id, dialogue_history=ticket_input.dialogue_history)
    await state.set_state(TicketFlow.in_dialogue)
    sessions.touch(ticket_input.telegram_id)
    await _run_decision_cycle(bot, api, state, message, ticket_id, ticket_input)


@router.message(TicketFlow.choosing_apparat)
@router.message(TicketFlow.choosing_problem)
@router.message(TicketFlow.intake_qa)
async def on_stray_text_during_menu(message: Message) -> None:
    await message.answer("Пожалуйста, выберите вариант кнопкой выше 👆")
    sessions.touch(str(message.from_user.id))


@router.message(TicketFlow.describing)
async def on_description(message: Message, state: FSMContext, bot: Bot, api: PrintBoxAPIClient) -> None:
    if not message.text:
        # Voice/sticker/video/etc - don't silently create a ticket with an
        # empty description, ask for actual text instead.
        await message.answer("Пожалуйста, опишите проблему текстом.")
        sessions.touch(str(message.from_user.id))
        return

    data = await state.get_data()
    apparat_name_text = data.get("apparat_name_text") or ""
    problem_type = data["problem_type"]

    # If we got here via a sub-issue button (e.g. "QR-код не появился на экране")
    # that was reclassified as non-technical, don't lose that context - it's not
    # otherwise captured anywhere once problem_type is reassigned to "other".
    intake = data.get("intake", {})
    context_prefix = intake.get("payment_issue") or intake.get("situation")
    user_text = _clip_free_text(message.text)

    if intake.get("is_top_level_other") and _mentions_payment_or_print(user_text):
        # The free text under the generic "Другая проблема" actually sounds like
        # a payment/print issue - worth checking against real data instead of
        # guessing blind from text alone. Route into the same when/receipt flow
        # used for "Не печатает", carrying the original text as context.
        await _update_intake(state, situation=user_text)
        await state.update_data(problem_type="not_printed")
        await state.set_state(TicketFlow.intake_qa)
        await message.answer(
            "Похоже, это может быть связано с оплатой или печатью — уточню пару деталей, "
            "чтобы проверить по нашим данным.\n\nКогда это случилось?",
            reply_markup=_when_keyboard(),
        )
        sessions.touch(str(message.from_user.id))
        return

    raw_text = f"{context_prefix}. {user_text}" if context_prefix else user_text

    ticket_input = diagnosis.TicketInput(
        problem_type=problem_type,
        apparat_name_text=apparat_name_text,
        telegram_id=str(message.from_user.id),
        username=message.from_user.username,
        contact=message.contact.phone_number if message.contact else None,
        raw_text=raw_text,
        submitted_at=tz.now(),
        dialogue_history=[f"user: {raw_text}"],
    )
    ticket_id = await storage.create_ticket(
        telegram_id=ticket_input.telegram_id,
        username=ticket_input.username,
        contact=ticket_input.contact,
        problem_type=problem_type,
        apparat_name=apparat_name_text,
        raw_text=ticket_input.raw_text,
    )
    await state.update_data(ticket_id=ticket_id, dialogue_history=ticket_input.dialogue_history)
    await state.set_state(TicketFlow.in_dialogue)
    sessions.touch(ticket_input.telegram_id)
    await _acknowledge(bot, message)
    await _run_decision_cycle(bot, api, state, message, ticket_id, ticket_input)


async def _run_decision_cycle(
    bot: Bot,
    api: PrintBoxAPIClient,
    state: FSMContext,
    message: Message,
    ticket_id: int,
    ticket_input: diagnosis.TicketInput,
) -> None:
    # Purely narrative pacing for the user, not a live readout of what's
    # actually happening under the hood (the real work all runs in one
    # gather_evidence() call below) - kept deliberately generic/non-technical
    # (no "SNMP"/"логи"/"принтер") so it reads as a normal status update.
    is_slow = ticket_input.problem_type in diagnosis.TECHNICAL_PROBLEM_TYPES
    status_message = None
    if is_slow:
        status_message = await message.answer(
            f"🔍 Заявка #{ticket_id}\nНачинаю проверку — обычно это занимает 1-5 минут ⏳",
            reply_markup=_cancel_keyboard(),
        )
        await asyncio.sleep(_STAGE_PAUSE_SECONDS)
        await status_message.edit_text(
            f"🔍 Заявка #{ticket_id}\n📄 Проверяю данные вашего обращения в системе...",
            reply_markup=_cancel_keyboard(),
        )
        await asyncio.sleep(_STAGE_PAUSE_SECONDS)
        await status_message.edit_text(
            f"🔍 Заявка #{ticket_id}\n✅ Данные проверены\n💳 Проверяю информацию об оплате...",
            reply_markup=_cancel_keyboard(),
        )

    try:
        evidence = await diagnosis.gather_evidence(api, ticket_input)
    except PrintBoxAPIError:
        logger.exception("evidence gathering failed for ticket %s", ticket_id)
        await _escalate(bot, api, message, ticket_id, None, Decision(
            action="escalate",
            reason="diagnosis_failed",
            staff_summary="Не удалось собрать диагностику (ошибка API) - нужна ручная проверка.",
        ))
        sessions.forget(ticket_input.telegram_id)
        await state.clear()
        return

    if status_message is not None:
        await asyncio.sleep(_STAGE_PAUSE_SECONDS)
        await status_message.edit_text(
            f"🔍 Заявка #{ticket_id}\n✅ Оплата проверена\n🧠 Анализирую все данные...",
            reply_markup=_cancel_keyboard(),
        )

    # The user may have pressed "❌ Отменить заявку" while the steps above were
    # running (a concurrent callback handler) - don't act on a cancelled ticket.
    current = await storage.get_ticket(ticket_id)
    if current is not None and current.status == "cancelled_by_user":
        return

    if evidence.transaction is not None:
        await storage.set_ticket_transaction(ticket_id, evidence.transaction.id)
        evidence.already_refunded = await storage.was_already_refunded(evidence.transaction.id)
    elif is_slow:
        await _handle_no_matching_order(bot, api, state, message, ticket_id, ticket_input, evidence)
        return

    decision = await ai_decider.decide(evidence)
    review = review_refund_case(evidence) if decision.action == "recommend_refund" else None

    if status_message is not None:
        await status_message.edit_text(f"🔍 Заявка #{ticket_id}\n✅ Анализ завершён")

    await storage.record_decision(
        ticket_id,
        evidence.to_dict(),
        decision.action,
        decision.reason,
        ",".join(review.blockers) if review and review.blockers else None,
        decision.action,
    )

    if decision.action == "recommend_refund":
        # The AI never refunds - this hands staff a prepared case to approve.
        await message.answer(
            _refund_pending_message(ticket_id), reply_markup=_main_menu_keyboard()
        )
        await _escalate(bot, api, message, ticket_id, evidence, decision, review)
        sessions.forget(ticket_input.telegram_id)
        await state.clear()
    elif decision.action in ("give_advice", "ask_clarifying_question"):
        is_advice = decision.action == "give_advice"
        await message.answer(
            decision.user_message,
            reply_markup=_feedback_keyboard(ticket_id) if is_advice else _cancel_keyboard(),
        )
        data = await state.get_data()
        dialogue_history = data.get("dialogue_history", [])
        dialogue_history.append(f"bot: {decision.user_message}")
        rounds = data.get("rounds", 0) + 1
        await state.update_data(rounds=rounds, dialogue_history=dialogue_history)
        sessions.touch(ticket_input.telegram_id)
        if rounds >= _MAX_AI_ROUNDS:
            await _escalate(
                bot, api, message, ticket_id, evidence,
                Decision(action="escalate", reason="ai_rounds_exhausted",
                         staff_summary="ИИ несколько раз советовал/уточнял, но вопрос не "
                         "закрылся, нужен человек."),
            )
            sessions.forget(ticket_input.telegram_id)
            await state.clear()
    else:
        # Prefer whatever concrete thing the AI found (a confirmed print, a
        # working apparat nearby) over the generic "нужен человек" line.
        text = decision.user_message or _escalate_user_message(ticket_id)
        if decision.user_message:
            text = f"🟡 Заявка #{ticket_id}: {decision.user_message}\n\nПередал сотруднику — отвечу здесь."
        await message.answer(text, reply_markup=_main_menu_keyboard())
        await _escalate(bot, api, message, ticket_id, evidence, decision)
        sessions.forget(ticket_input.telegram_id)
        await state.clear()


async def _handle_no_matching_order(
    bot: Bot,
    api: PrintBoxAPIClient,
    state: FSMContext,
    message: Message,
    ticket_id: int,
    ticket_input: diagnosis.TicketInput,
    evidence: diagnosis.Evidence,
) -> None:
    """No transaction from this telegram_id (or matching the hints given) was found
    at all - tell the user plainly instead of quietly escalating a "maybe" case,
    mention whether we even see an uploaded file from them (helps tell "wrong
    account" apart from "never actually sent anything"), and give them one more
    chance to provide a receipt in case they wrote from a different Telegram
    account."""
    has_document = await diagnosis.has_recent_document(api, ticket_input.telegram_id, tz.now())
    # has_document can be None if the documents check itself failed (that API
    # is known to occasionally 500) - only make the claim when we're actually
    # sure there's no file, never when we simply couldn't check.
    document_note = (
        "Файл от вашего аккаунта за последние сутки мы тоже не видим. "
        if has_document is False
        else ""
    )

    if ticket_input.receipt_photo_file_id:
        await message.answer(
            f"🔍 Заявка #{ticket_id}: не нашёл заказов от вашего Telegram-аккаунта в системе. "
            f"{document_note}Чек, который вы прислали, передаю сотруднику — он проверит вручную.",
            reply_markup=_main_menu_keyboard(),
        )
        # Passing `evidence` (not None) so notify.send_escalation forwards the
        # receipt to staff in the right format (photo vs PDF document).
        await _escalate(
            bot, api, message, ticket_id, evidence,
            Decision(action="escalate", reason="no_matching_order_with_receipt",
                     staff_summary=f"Заявка #{ticket_id}: не нашли заказ юзера в системе; "
                     f"{'файла от него тоже не нашли; ' if has_document is False else ''}"
                     "юзер приложил чек - нужна ручная проверка."),
        )
        sessions.forget(ticket_input.telegram_id)
        await state.clear()
        return

    await state.update_data(awaiting_post_diagnosis_receipt=True, ticket_id=ticket_id)
    await state.set_state(TicketFlow.awaiting_receipt_photo)
    await message.answer(
        f"🔍 Заявка #{ticket_id}: не нашёл оплаты от вашего Telegram-аккаунта в системе за "
        f"указанное время. {document_note}Возможно, вы писали/оплачивали с другого аккаунта — "
        "пришлите, пожалуйста, чек оплаты, и я передам сотруднику для проверки.",
        reply_markup=_no_match_receipt_keyboard(),
    )
    sessions.touch(ticket_input.telegram_id)


async def _acknowledge(bot: Bot, message: Message) -> None:
    """A 👀 on the user's own message instead of another "принято в обработку"
    line - quieter, and it stays attached to what it acknowledges. Best-effort:
    reactions can be unavailable, and that must not derail the ticket."""
    try:
        await bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji="👀")],
        )
    except Exception:
        logger.debug("could not react to message %s", message.message_id, exc_info=True)


def _refund_pending_message(ticket_id: int) -> str:
    return (
        f"🟠 Заявка #{ticket_id}: нашёл признаки технической ошибки и подготовил возврат — "
        "его подтверждает сотрудник, отвечу здесь, как только решение будет."
    )


def _escalate_user_message(ticket_id: int) -> str:
    return (
        f"🟡 Заявка #{ticket_id}: ситуация неоднозначная, нужен взгляд человека — уже "
        "передал ему все детали, отвечу здесь."
    )


async def _escalate(
    bot: Bot,
    api: PrintBoxAPIClient,
    message: Message,
    ticket_id: int,
    evidence,
    decision: Decision,
    review: RefundReview | None = None,
) -> None:
    await storage.set_ticket_status(ticket_id, "escalated")
    if evidence is not None:
        await notify.send_escalation(
            bot, settings.support_staff_chat_id, ticket_id, evidence, decision, review
        )
    else:
        # No Evidence object here (scripted reply, or diagnosis failed before
        # one was built) - notify still pulls what we stored for this ticket so
        # staff get apparat/contact/the original complaint, not one bare line.
        await notify.send_plain_escalation(bot, settings.support_staff_chat_id, ticket_id, decision)


@router.callback_query(F.data.startswith("feedback:"))
async def on_feedback(callback: CallbackQuery, state: FSMContext, bot: Bot, api: PrintBoxAPIClient) -> None:
    _, outcome, ticket_id_str = callback.data.split(":")
    ticket_id = int(ticket_id_str)
    telegram_id = str(callback.from_user.id)

    if outcome == "helped":
        await storage.set_ticket_status(ticket_id, "resolved_advice")
        await callback.message.edit_text(
            callback.message.text + "\n\n👍 Рад был помочь!", reply_markup=_main_menu_keyboard()
        )
        await csat.send_poll(bot, callback.from_user.id, ticket_id)
        sessions.forget(telegram_id)
        await state.clear()
    else:
        # Don't reflexively escalate on every "не помогло" tap - ask what's
        # actually still wrong first. Most of the time the AI can resolve it
        # right here; only a real money/technical/serious case should reach
        # the staff group (see ai_decider.decide_followup's strictness).
        original_reply = callback.message.text
        await callback.message.edit_text(
            original_reply + "\n\n😕 Уточните, пожалуйста, что именно не так — посмотрю ещё раз.",
            reply_markup=None,
        )
        await state.update_data(ticket_id=ticket_id, pending_feedback_original_reply=original_reply)
        await state.set_state(TicketFlow.awaiting_nothelped_detail)
        sessions.touch(telegram_id)
    await callback.answer()


@router.message(TicketFlow.awaiting_nothelped_detail)
async def on_nothelped_detail_provided(
    message: Message, state: FSMContext, bot: Bot, api: PrintBoxAPIClient
) -> None:
    if not message.text:
        await message.answer("Пожалуйста, напишите текстом, что именно не так.")
        sessions.touch(str(message.from_user.id))
        return

    data = await state.get_data()
    ticket_id = data["ticket_id"]
    original_reply = data.get("pending_feedback_original_reply", "")
    telegram_id = str(message.from_user.id)
    ticket_record = await storage.get_ticket(ticket_id)

    status_message = await message.answer("🤔 Думаю над вашим ответом...")
    await asyncio.sleep(_STAGE_PAUSE_SECONDS)
    await status_message.edit_text("🤔 Думаю над вашим ответом...\n🧠 Сравниваю с обращением...")
    await asyncio.sleep(_STAGE_PAUSE_SECONDS)

    followup = await ai_decider.decide_followup(
        original_reply=original_reply,
        problem_type=ticket_record.problem_type if ticket_record else "other",
        raw_text=ticket_record.raw_text if ticket_record else "",
        user_followup=_clip_free_text(message.text),
    )

    if followup.action == "resolve":
        await status_message.edit_text("🤔 Думаю над вашим ответом...\n✅ Готово")
        await storage.set_ticket_status(ticket_id, "resolved_advice")
        await message.answer(followup.reply_text, reply_markup=_main_menu_keyboard())
        await csat.send_poll(bot, message.from_user.id, ticket_id)
        sessions.forget(telegram_id)
        await state.clear()
        return

    await status_message.edit_text("🤔 Думаю над вашим ответом...\n✅ Уточняю детали")
    await state.update_data(
        pending_escalation_ticket_id=ticket_id,
        pending_escalation_reason=followup.reason,
        pending_escalation_summary=followup.staff_summary,
    )

    if ticket_record is not None and not ticket_record.payment_expected:
        # The complaint is about something that happens before paying (QR never
        # appeared, bank refused, file never uploaded) - there is no receipt to
        # ask for, and asking anyway reads as not having listened.
        await _proceed_after_escalation_receipt(message, state, bot, api, telegram_id)
        return

    await state.set_state(TicketFlow.awaiting_escalation_receipt)
    await message.answer(
        "Если есть чек оплаты — пришлите, поможет сотруднику быстрее разобраться.",
        reply_markup=_escalation_receipt_keyboard(),
    )


@router.callback_query(F.data == "escreceipt:photo", TicketFlow.awaiting_escalation_receipt)
async def on_escalation_receipt_photo_requested(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Пришлите, пожалуйста, чек оплаты — фото или PDF.")
    sessions.touch(str(callback.from_user.id))
    await callback.answer()


@router.message(TicketFlow.awaiting_escalation_receipt, F.photo | F.document)
async def on_escalation_receipt_provided(
    message: Message, state: FSMContext, bot: Bot, api: PrintBoxAPIClient
) -> None:
    file_id, is_document, _ = await _extract_receipt_data(bot, message)
    if file_id:
        await state.update_data(
            pending_escalation_receipt_file_id=file_id, pending_escalation_receipt_is_document=is_document
        )
    await _proceed_after_escalation_receipt(message, state, bot, api, str(message.from_user.id))


@router.callback_query(F.data == "escreceipt:skip", TicketFlow.awaiting_escalation_receipt)
async def on_escalation_receipt_skipped(
    callback: CallbackQuery, state: FSMContext, bot: Bot, api: PrintBoxAPIClient
) -> None:
    await _proceed_after_escalation_receipt(callback.message, state, bot, api, str(callback.from_user.id))
    await callback.answer()


@router.message(TicketFlow.awaiting_escalation_receipt)
async def on_escalation_receipt_expected_but_text_sent(message: Message) -> None:
    await message.answer("Пришлите, пожалуйста, чек (фото или PDF), или нажмите «🤷 Нет чека» выше.")
    sessions.touch(str(message.from_user.id))


async def _proceed_after_escalation_receipt(
    message: Message, state: FSMContext, bot: Bot, api: PrintBoxAPIClient, telegram_id: str
) -> None:
    data = await state.get_data()
    ticket_id = data["pending_escalation_ticket_id"]
    ticket_record = await storage.get_ticket(ticket_id)
    receipt_file_id = data.get("pending_escalation_receipt_file_id")
    receipt_is_document = data.get("pending_escalation_receipt_is_document", False)

    if ticket_record is not None and ticket_record.contact:
        # Already have a way to reach them (e.g. shared earlier) - no need to ask again.
        await _escalate(
            bot, api, message, ticket_id, None,
            Decision(action="escalate", reason=data["pending_escalation_reason"],
                     staff_summary=data["pending_escalation_summary"]),
        )
        if receipt_file_id:
            await _forward_receipt_to_staff(bot, ticket_id, receipt_file_id, receipt_is_document)
        sessions.forget(telegram_id)
        await state.clear()
        await message.answer("Если что-то ещё понадобится — вот меню:", reply_markup=_main_menu_keyboard())
        return

    # Some users have no Telegram username and only show up as a bare numeric
    # ID - staff can't reach those directly, so get a phone number first.
    await state.set_state(TicketFlow.awaiting_phone_for_escalation)
    await message.answer(
        "Чтобы сотрудник мог с вами связаться, оставьте, пожалуйста, номер телефона 👇",
        reply_markup=_phone_request_keyboard(),
    )


@router.message(TicketFlow.awaiting_phone_for_escalation)
async def on_phone_for_escalation_provided(
    message: Message, state: FSMContext, bot: Bot, api: PrintBoxAPIClient
) -> None:
    data = await state.get_data()
    ticket_id = data["pending_escalation_ticket_id"]
    telegram_id = str(message.from_user.id)

    phone = _resolve_phone_input(
        message.contact.phone_number if message.contact else None, message.text
    )

    if not phone:
        await message.answer(
            "Не получилось распознать номер. Пожалуйста, отправьте номер телефона — это "
            "нужно, чтобы сотрудник мог с вами связаться.",
            reply_markup=_phone_request_keyboard(),
        )
        return

    await storage.set_ticket_contact(ticket_id, phone)
    await message.answer("Спасибо! Передаю сотруднику.", reply_markup=ReplyKeyboardRemove())

    await _escalate(
        bot, api, message, ticket_id, None,
        Decision(
            action="escalate",
            reason=data["pending_escalation_reason"],
            staff_summary=data["pending_escalation_summary"],
        ),
    )
    receipt_file_id = data.get("pending_escalation_receipt_file_id")
    if receipt_file_id:
        await _forward_receipt_to_staff(
            bot, ticket_id, receipt_file_id, data.get("pending_escalation_receipt_is_document", False)
        )
    sessions.forget(telegram_id)
    await state.clear()
    await message.answer("Если что-то ещё понадобится — вот меню:", reply_markup=_main_menu_keyboard())


@router.callback_query(F.data == "cancel")
async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    if ticket_id:
        await storage.set_ticket_status(ticket_id, "cancelled_by_user")
    await state.clear()
    sessions.forget(str(callback.from_user.id))
    await callback.message.edit_text(
        "Заявка отменена. Если что-то ещё понадобится — вот меню:",
        reply_markup=_main_menu_keyboard(),
    )
    await callback.answer()


@router.message(TicketFlow.in_dialogue)
async def on_followup_message(message: Message, state: FSMContext, bot: Bot, api: PrintBoxAPIClient) -> None:
    data = await state.get_data()
    ticket_id = data["ticket_id"]
    dialogue_history = data.get("dialogue_history", [])
    dialogue_history.append(f"user: {message.text}")

    ticket_record = await storage.get_ticket(ticket_id)
    ticket_input = diagnosis.TicketInput(
        problem_type=ticket_record.problem_type,
        apparat_name_text=ticket_record.apparat_name or "",
        telegram_id=ticket_record.telegram_id,
        username=ticket_record.username,
        contact=ticket_record.contact,
        raw_text=ticket_record.raw_text or "",
        submitted_at=tz.now(),
        dialogue_history=dialogue_history,
    )
    await state.update_data(dialogue_history=dialogue_history)
    sessions.touch(ticket_input.telegram_id)
    await _run_decision_cycle(bot, api, state, message, ticket_id, ticket_input)


@router.message(StateFilter(None))
async def on_live_chat_message(message: Message, bot: Bot) -> None:
    """While a staff member has the dialogue open, everything the user writes
    goes to them - not to the assistant. Registered before the receipt and
    concierge handlers so a real conversation is never answered by a model."""
    ticket = await storage.find_live_chat_ticket(str(message.from_user.id))
    if ticket is None:
        raise SkipHandler
    try:
        await notify.relay_user_message(bot, ticket, message)
    except Exception:
        logger.exception("could not relay user message for ticket %s", ticket.id)
        await message.answer("Не получилось передать сообщение, попробуйте ещё раз.")


@router.message(StateFilter(None), F.photo | F.document)
async def on_requested_receipt(message: Message, bot: Bot) -> None:
    """A receipt arriving out of the blue, with no active flow - this happens
    when staff tapped "Запросить чек" and the user answers later, long after
    their session was cleared. Without this the photo would fall through to the
    concierge, which has no idea a ticket is waiting for it."""
    telegram_id = str(message.from_user.id)
    ticket = await storage.find_ticket_awaiting_receipt(telegram_id)
    if ticket is None:
        await message.answer(
            "Если это чек по заявке — начните, пожалуйста, с /start, так я смогу его привязать.",
            reply_markup=_main_menu_keyboard(),
        )
        return

    file_id, is_document, _ = await _extract_receipt_data(bot, message)
    if not file_id:
        await message.answer("Не получилось прочитать файл, пришлите, пожалуйста, ещё раз.")
        return

    await notify.forward_receipt(bot, ticket.id, file_id, is_document)
    await storage.set_ticket_status(ticket.id, "escalated")
    await message.answer(
        f"Спасибо! Передал чек сотруднику по заявке #{ticket.id} — вернусь с ответом сюда.",
        reply_markup=_main_menu_keyboard(),
    )


_UNKNOWN_COMMAND_REPLY = (
    "🤔 Такой команды не знаю. Воспользуйтесь меню ниже или напишите /start, чтобы открыть его заново."
)


@router.message()
async def on_unstructured_message(message: Message) -> None:
    text = message.text or ""
    if text.startswith("/"):
        # Any slash-command other than /start or /admin lands here unhandled -
        # always the same canned reply, no AI call: a typo'd/made-up command
        # isn't a real question, and burning a model call on every "/privet"
        # is pure waste.
        await message.answer(_UNKNOWN_COMMAND_REPLY, reply_markup=_main_menu_keyboard())
        return
    reply = await concierge.answer(text)
    await message.answer(reply, reply_markup=_main_menu_keyboard())
