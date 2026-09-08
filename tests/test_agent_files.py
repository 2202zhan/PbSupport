"""Phase 6: a receipt, whenever it arrives.

The menu bot could only take a receipt in the one state that had asked for one;
anywhere else it answered "напишите текстом" to someone who had just sent proof
of payment. Here a file is read the moment it lands.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

import receipt_parser
import receipts
import storage
import tz
from agent import files, memory


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage.settings, "support_bot_db_path", str(tmp_path / "t.sqlite3"))
    storage.init_db()


def _message(photo=False, caption=None):
    return SimpleNamespace(
        document=None if photo else SimpleNamespace(
            file_id="doc-1", mime_type="application/pdf", file_name="receipt.pdf"
        ),
        photo=[SimpleNamespace(file_id="photo-1")] if photo else None,
        caption=caption,
    )


def _parsed(monkeypatch, amount=80.0, minutes_ago=5):
    async def _extract(_bot, _message):
        return "doc-1", True, receipt_parser.ReceiptData(
            amount=amount, paid_at=tz.now() - timedelta(minutes=minutes_ago), receipt_number="QR1"
        )

    monkeypatch.setattr(receipts, "extract", _extract)


async def test_a_readable_receipt_becomes_a_fact_in_the_conversation(db, monkeypatch):
    _parsed(monkeypatch)
    conversation = await memory.current_conversation("1")
    fact = await files.read_and_remember(None, _message(), conversation.id)
    assert "80 ₸" in fact and "чек" in fact


async def test_the_file_is_kept_so_staff_can_look_at_it(db, monkeypatch):
    # A summary of a receipt is not a receipt.
    _parsed(monkeypatch)
    conversation = await memory.current_conversation("1")
    await files.read_and_remember(None, _message(), conversation.id)
    import json

    stored = json.loads((await storage.get_conversation(conversation.id)).receipt)
    assert stored["file_id"] == "doc-1" and stored["is_document"] is True
    assert stored["amount"] == 80.0


async def test_a_photo_is_accepted_and_honestly_marked_unread(db, monkeypatch):
    # There is no OCR here; letting the model assume the numbers were read is
    # worse than saying they weren't.
    async def _extract(_bot, _message):
        return "photo-1", False, None

    monkeypatch.setattr(receipts, "extract", _extract)
    conversation = await memory.current_conversation("1")
    fact = await files.read_and_remember(None, _message(photo=True), conversation.id)
    assert "прочитать не удалось" in fact
    assert "сотрудник посмотрит" in fact
    assert (await storage.get_conversation(conversation.id)).receipt


async def test_a_receipt_from_last_week_is_called_out(db, monkeypatch):
    # The receipt is the one thing that can reveal the real date when the
    # user's own answer suggested otherwise.
    _parsed(monkeypatch, minutes_ago=60 * 72)
    conversation = await memory.current_conversation("1")
    fact = await files.read_and_remember(None, _message(), conversation.id)
    assert "больше суток назад" in fact
    assert "проверить уже не можем" in fact


async def test_a_caption_is_carried_along(db, monkeypatch):
    async def _extract(_bot, _message):
        return "photo-1", False, None

    monkeypatch.setattr(receipts, "extract", _extract)
    conversation = await memory.current_conversation("1")
    fact = await files.read_and_remember(None, _message(photo=True, caption="вот оплата"), conversation.id)
    assert "вот оплата" in fact


async def test_a_file_that_will_not_open_says_so(db, monkeypatch):
    async def _extract(_bot, _message):
        return "", False, None

    monkeypatch.setattr(receipts, "extract", _extract)
    conversation = await memory.current_conversation("1")
    fact = await files.read_and_remember(None, _message(), conversation.id)
    assert "не получилось открыть" in fact
    assert (await storage.get_conversation(conversation.id)).receipt is None


async def test_the_receipt_overrides_a_guessed_time_in_the_investigation(db, monkeypatch):
    # Someone who says "час назад" but whose receipt says 13:42 is telling us
    # 13:42 - and an exact pair is precise enough to search on.
    from agent.reading import INVESTIGATE_ORDER
    from agent.types import TurnContext

    _parsed(monkeypatch, amount=80.0, minutes_ago=5)
    conversation = await memory.current_conversation("884013433")
    await files.read_and_remember(None, _message(), conversation.id)

    seen = {}

    async def _gather(_api, ticket):
        seen["ticket"] = ticket
        raise SystemExit  # far enough: we only care what was asked

    monkeypatch.setattr("diagnosis.gather_evidence", _gather)
    ctx = TurnContext("884013433", "zhan", conversation.id, "не вышло", api=None)
    with pytest.raises(SystemExit):
        await INVESTIGATE_ORDER.run({"when": "вчера"}, ctx)

    assert seen["ticket"].manual_hint_is_precise is True
    assert seen["ticket"].manual_hint_amount == 80.0
    assert (tz.now() - seen["ticket"].manual_hint_time) < timedelta(minutes=10)
