"""Tools that look things up.

Each one answers in plain Russian and puts the numbers behind its answer into
ctx.staff_notes. That split is the whole point: the model cannot leak a toner
percentage into a reply if it was never shown one, and a person reading the
escalation card still gets the exact reading.
"""

import logging

import apparats
import diagnosis
import guard_rules
import tz
from agent.registry import ToolError, ToolSpec
from agent.types import TurnContext
from api_client import PrintBoxAPIError
from datetime import datetime  # noqa: F401  (used in a string annotation)

logger = logging.getLogger(__name__)

_TOPICS = {
    "hours": (
        "Киоски включены с 8:00 до 19:00 по времени Астаны, ночью выключены — распечатать "
        "в это время нельзя. Бот и заявки работают круглосуточно."
    ),
    "formats": (
        "Принимаем PDF, DOC/DOCX и изображения (JPG/PNG), размер файла — примерно до 20 МБ. "
        "Отправлять надо именно файлом, а не «фото»: фото Telegram сжимает и формат теряется."
    ),
    "prices": (
        "Точную стоимость показывает основной бот PrintBox после загрузки файла, а также "
        "экран киоска перед оплатой — она зависит от настроек печати. Своей таблицы цен у "
        "тебя нет, не называй цифры."
    ),
    "how_it_works": (
        "Юзер отправляет документ в основной бот PrintBox, получает 4-значный код (живёт "
        "около 15 минут), вводит его на экране киоска, выбирает настройки печати, "
        "подтверждает и платит по QR через Kaspi. После оплаты киоск печатает сам."
    ),
    "refunds": (
        "Если деньги списались, а печать не вышла — это повод на возврат. Возврат "
        "подтверждает живой сотрудник, у бота такой возможности нет. Заявки старше суток "
        "проверить уже нельзя: технические данные за тот период не сохраняются."
    ),
    "payment": (
        "Оплата только по QR через Kaspi. Других способов нет. Ошибки на стороне "
        "банковского приложения мы исправить не можем — это к поддержке банка."
    ),
}


async def _service_info(args: dict, _ctx: TurnContext) -> dict:
    topic = args.get("topic")
    if topic not in _TOPICS:
        raise ToolError(f"неизвестная тема {topic!r}, доступны: {', '.join(_TOPICS)}")
    return {"тема": topic, "факт": _TOPICS[topic]}


SERVICE_INFO = ToolSpec(
    name="service_info",
    description=(
        "Выверенный факт о сервисе: часы работы, форматы файлов, цены, как всё устроено, "
        "возвраты, оплата. Спрашивай, вместо того чтобы вспоминать — здесь актуальное."
    ),
    parameters={
        "type": "object",
        "properties": {"topic": {"type": "string", "enum": list(_TOPICS)}},
        "required": ["topic"],
    },
    terminal=False,
    run=_service_info,
)


_VERDICTS = {
    apparats.ASLEEP: "аппараты сейчас выключены (работают с 8:00 до 19:00), показаний нет",
    apparats.CRITICAL: "на аппарате заканчиваются бумага или тонер, либо он сообщает об ошибке",
    apparats.HEALTHY: "аппарат не сообщает ни о нехватке бумаги или тонера, ни об ошибках",
    apparats.UNKNOWN: "показаний от аппарата сейчас нет",
}


async def _check_apparat(args: dict, ctx: TurnContext) -> dict:
    place = (args.get("place") or "").strip()
    if not place:
        return await _ask_where(ctx, "юзер не сказал, о каком аппарате речь")

    verdict, staff_note, found = await apparats.read_supplies_detailed(ctx.api, place)
    # Name the machine the way our own records name it, not the way the model
    # paraphrased the user - otherwise a reading from one kiosk gets reported
    # confidently about another.
    named = f"{found.name_apparat} ({found.address})" if found and found.address else (
        found.name_apparat if found else place
    )
    ctx.staff_notes.append(f"Аппарат «{named}» (искали по «{place}»): {staff_note}")

    answer = {
        "аппарат": named,
        "состояние": _VERDICTS[verdict],
        "требует_вмешательства": verdict == apparats.CRITICAL,
    }
    if verdict == apparats.UNKNOWN:
        # Two very different reasons look the same from here, and only one of
        # them is worth asking the user about.
        if found is None:
            return await _ask_where(ctx, f"по названию «{place}» аппарат не нашёлся")
    if verdict in (apparats.CRITICAL, apparats.UNKNOWN):
        alternate = await apparats.suggest_alternate(ctx.api, place)
        if alternate:
            answer["рядом_работает"] = alternate
    return answer


async def _ask_where(ctx: TurnContext, why: str) -> dict:
    """Hands back the places we actually have, so the next question offers real
    options. Left to itself the model invents a building it was never told
    about and then reports on a machine nobody asked about."""
    return {
        "нужно_уточнить": why,
        "наши_точки": await apparats.known_places(ctx.api),
        "как_быть": "спроси у юзера, где стоит аппарат, и предложи эти точки кнопками",
    }


CHECK_APPARAT = ToolSpec(
    name="check_apparat",
    description=(
        "Что аппарат сообщает о себе прямо сейчас: бумага, тонер, ошибки. Вызывай на любой "
        "жалобе про железо или качество печати, прежде чем что-то предполагать. Принимает "
        "название или место так, как их назвал юзер. Ничего не придумывай: если он не "
        "сказал, где аппарат, вызови без place — вернётся список наших точек, спроси по нему. "
        "Цифры инструмент не возвращает и юзеру они не нужны — тебе нужен вывод."
    ),
    parameters={
        "type": "object",
        "properties": {
            "place": {
                "type": "string",
                "description": (
                    "Название аппарата или корпус — дословно теми словами, которыми "
                    "написал юзер, без пересказа и без подстановки варианта из твоего "
                    "прошлого вопроса. Сказал «аппарат 3» — так и передай. Не указывай "
                    "вовсе, если он про аппарат ничего не говорил."
                ),
            }
        },
    },
    terminal=False,
    run=_check_apparat,
)


async def _find_my_orders(_args: dict, ctx: TurnContext) -> dict:
    limit = 5
    try:
        transactions = await ctx.api.get_transactions(telegram_id=ctx.telegram_id, per_page=20)
    except PrintBoxAPIError:
        logger.warning("could not read orders for %s", ctx.telegram_id)
        return {"ошибка": "не получилось посмотреть заказы, попробуй ещё раз или зови человека"}

    if not transactions:
        return {
            "заказов": 0,
            "вывод": (
                "оплат с этого telegram-аккаунта не найдено. Возможно, платили с другого "
                "аккаунта — можно попросить чек"
            ),
        }

    recent = sorted(transactions, key=lambda t: t.date, reverse=True)[:limit]
    ctx.staff_notes.append(
        "Заказы юзера: " + "; ".join(f"{t.id} {t.date:%d.%m %H:%M} {t.amount} ₸ {t.status}" for t in recent)
    )
    return {
        "заказов": len(recent),
        "список": [
            {
                "когда": _when(t.date),
                "аппарат": t.machine,
                "сумма": f"{t.amount:g} ₸",
                "статус": t.status,
            }
            for t in recent
        ],
    }


def _when(moment) -> str:
    delta = tz.now() - moment
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    if delta.days < 1:
        return f"сегодня в {moment:%H:%M}"
    if delta.days < 2:
        return f"вчера в {moment:%H:%M}"
    return f"{moment:%d.%m в %H:%M}"


FIND_MY_ORDERS = ToolSpec(
    name="find_my_orders",
    description=(
        "Последние оплаты этого юзера: когда, на каком аппарате, на сколько. Вызывай, когда "
        "речь про деньги или «оплатил, а не вышло» — сначала посмотри, потом спрашивай."
    ),
    parameters={"type": "object", "properties": {}},
    terminal=False,
    run=_find_my_orders,
)


# What the evidence pipeline found, said the way a person would say it. The
# model is given the conclusion; SNMP, device logs and neighbour scans are ours
# and stay in the staff note.
def _print_verdict(evidence) -> str:
    if evidence.log_download_error:
        return "аппарат не смог скачать файл с сервера — печать не началась, и это наша ошибка"
    if evidence.print_signal_confirmed is False:
        return "принтер не начинал печать по этому заказу"
    if evidence.log_print_success:
        return "печать по этому заказу прошла до конца"
    if evidence.print_signal_confirmed:
        return "принтер сообщает, что печать прошла"
    return "по этому заказу техника ничего определённого не говорит"


_BLOCKER_WORDS = {
    "no_transaction_matched": "оплата под описание не нашлась",
    "already_refunded": "по этому заказу возврат уже делали",
    "identity_unconfirmed": "оплата с этого telegram-аккаунта не подтверждается",
    "transaction_ambiguous": "под описание подходит несколько оплат, какая именно — неясно",
}


async def _investigate_order(args: dict, ctx: TurnContext) -> dict:
    if ctx.on_progress:
        await ctx.on_progress("🔍 Проверяю заказ по нашим данным…")

    hint_time = _parse_when(args.get("when"))
    ticket = diagnosis.TicketInput(
        problem_type="not_printed",
        apparat_name_text=(args.get("apparat") or "").strip(),
        telegram_id=ctx.telegram_id,
        username=ctx.username,
        contact=None,
        raw_text=ctx.user_message,
        submitted_at=tz.now(),
        manual_hint_amount=_as_amount(args.get("amount")),
        manual_hint_time=hint_time,
    )
    try:
        evidence = await diagnosis.gather_evidence(ctx.api, ticket)
    except PrintBoxAPIError:
        logger.exception("investigation failed for %s", ctx.telegram_id)
        return {"ошибка": "проверка не прошла — данных нет, зови человека"}

    review = guard_rules.review_refund_case(evidence)
    ctx.staff_notes.append(_evidence_note(evidence, review))

    transaction = evidence.transaction
    if transaction is None:
        return {
            "оплата_найдена": False,
            "вывод": (
                "оплату под это описание найти не удалось. Уточни время и сумму или попроси "
                "чек — по чеку найдём точно"
            ),
            "возврат_возможен": False,
        }

    return {
        "оплата_найдена": True,
        "заказ": {
            "когда": _when(transaction.date),
            "аппарат": transaction.machine,
            "сумма": f"{transaction.amount:g} ₸",
        },
        "что_показала_техника": _print_verdict(evidence),
        "возврат_возможен": review.can_refund,
        "мешает": [_BLOCKER_WORDS.get(b, b) for b in review.blockers],
        "как_быть": (
            "похоже на нашу техническую ошибку — вызывай escalate, возврат подтвердит сотрудник"
            if review.can_refund
            else "сам возврат не предлагай: скажи, что нашёл, и передай сотруднику через escalate"
        ),
    }


def _evidence_note(evidence, review) -> str:
    t = evidence.transaction
    lines = [
        "Проверка заказа:",
        f"  транзакция: {t.id} {t.date:%d.%m %H:%M} {t.amount} ₸ {t.machine}" if t else "  транзакция: не найдена",
        f"  идентификация: {'подтверждена' if evidence.identity_confirmed else 'НЕТ'}",
        f"  SNMP подтвердил печать: {evidence.print_signal_confirmed}",
        f"  ошибка скачивания в логах: {evidence.log_download_error}",
        f"  логи: печать завершена: {evidence.log_print_success}",
        f"  документ найден: {evidence.document_found}",
    ]
    if evidence.mass_outage_suspected:
        lines.append(
            f"  ⚠️ подозрение на массовый сбой: {evidence.neighbor_failure_count}"
            f"/{evidence.neighbor_total_checked} соседних заказов"
        )
    if evidence.apparat_active_alert:
        lines.append(f"  ⚠️ аппарат сейчас: {evidence.apparat_active_alert}")
    lines.append(f"  возврат по правилам: {'можно' if review.can_refund else 'нельзя'}")
    if review.blockers:
        lines.append(f"  блокеры: {', '.join(review.blocker_labels)}")
    if review.warnings:
        lines.append(f"  предупреждения: {', '.join(review.warning_labels)}")
    return "\n".join(lines)


_WHEN_MINUTES = {
    "только что": 2, "just_now": 2,
    "минут 20 назад": 20, "recent": 20,
    "час назад": 60, "пару часов назад": 120, "hours": 120,
    "сегодня раньше": 300, "today": 300,
    "вчера": 60 * 24, "yesterday": 60 * 24,
}


def _parse_when(raw) -> "datetime | None":
    if not raw or not isinstance(raw, str):
        return None
    minutes = _WHEN_MINUTES.get(raw.strip().lower())
    if minutes is None:
        return None
    from datetime import timedelta

    return tz.now() - timedelta(minutes=minutes)


def _as_amount(raw) -> float | None:
    try:
        return float(str(raw).replace(",", ".").strip()) if raw not in (None, "") else None
    except ValueError:
        return None


INVESTIGATE_ORDER = ToolSpec(
    name="investigate_order",
    description=(
        "Полная проверка конкретного заказа: была ли оплата, дошёл ли файл до аппарата, "
        "печатал ли принтер, можно ли по нашим правилам возвращать деньги. Это главный "
        "инструмент по жалобе «оплатил, а не распечатал» — вызывай его, а не гадай. "
        "Работает несколько секунд. Чем точнее время и сумма, тем точнее найдём заказ."
    ),
    parameters={
        "type": "object",
        "properties": {
            "when": {
                "type": "string",
                "enum": ["только что", "минут 20 назад", "час назад", "пару часов назад",
                         "сегодня раньше", "вчера"],
                "description": "Когда это было, со слов юзера.",
            },
            "amount": {"type": "string", "description": "Сумма оплаты в тенге, если юзер назвал."},
            "apparat": {"type": "string", "description": "Аппарат или корпус, если юзер назвал."},
        },
    },
    terminal=False,
    run=_investigate_order,
)
