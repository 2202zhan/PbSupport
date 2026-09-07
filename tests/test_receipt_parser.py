from datetime import datetime

import receipt_parser

SAMPLE_RECEIPT_TEXT = """\
Разное
Аппарат самообслуживания svp-66739,0,vending machine,Вендинговый Аппарат,+77057280525
Платеж успешно совершен
35,00 ₸
№ квитанции QR16068964665
Идентификатор 231161099709
ID аппарата svp-66739
ID аппарата 0
Описание vending machine
Покупка в Вендинговый Аппарат
Телефон поддержки +77057280525
Дата и время по Астане 18.06.2026 11:58:12
ФИО плательщика Кулжанов Ж. Е.
Оплачено с Kaspi Gold
"""


def test_parses_amount_date_and_receipt_number():
    result = receipt_parser.parse_kaspi_receipt(SAMPLE_RECEIPT_TEXT)
    assert result.amount == 35.0
    assert result.paid_at == datetime(2026, 6, 18, 11, 58, 12)
    assert result.receipt_number == "QR16068964665"
    assert result.is_useful is True


def test_handles_larger_amount_with_thousands_separator():
    text = "Платеж успешно совершен\n1 250,50 ₸\nДата и время по Астане 01.01.2026 09:00:00"
    result = receipt_parser.parse_kaspi_receipt(text)
    assert result.amount == 1250.50


def test_empty_or_unrelated_text_yields_nothing_useful():
    result = receipt_parser.parse_kaspi_receipt("случайный текст без чека")
    assert result.amount is None
    assert result.paid_at is None
    assert result.is_useful is False


def test_extract_text_from_pdf_handles_garbage_gracefully():
    assert receipt_parser.extract_text_from_pdf(b"not a real pdf") == ""
