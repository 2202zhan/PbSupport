"""Parses Kaspi payment receipt PDFs (the standard "Платеж успешно совершен" export)
to pull out the exact amount and timestamp - far more reliable than asking the user
to recall/guess them via buttons. Pure regex over extracted text, no OCR: these are
text-based PDFs, not scanned images.
"""

import io
import re
from dataclasses import dataclass
from datetime import datetime

from pypdf import PdfReader

_AMOUNT_RE = re.compile(r"(\d[\d\s]*[.,]\d{2})\s*₸")
_DATETIME_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2}:\d{2})")
_RECEIPT_NO_RE = re.compile(r"квитанции\s*([A-Za-zА-Яа-я0-9]+)")


@dataclass
class ReceiptData:
    amount: float | None = None
    paid_at: datetime | None = None
    receipt_number: str | None = None

    @property
    def is_useful(self) -> bool:
        return self.amount is not None or self.paid_at is not None


def extract_text_from_pdf(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return ""


def parse_kaspi_receipt(text: str) -> ReceiptData:
    result = ReceiptData()

    amount_match = _AMOUNT_RE.search(text)
    if amount_match:
        raw = amount_match.group(1).replace(" ", "").replace(",", ".")
        try:
            result.amount = float(raw)
        except ValueError:
            pass

    dt_match = _DATETIME_RE.search(text)
    if dt_match:
        try:
            result.paid_at = datetime.strptime(f"{dt_match.group(1)} {dt_match.group(2)}", "%d.%m.%Y %H:%M:%S")
        except ValueError:
            pass

    receipt_match = _RECEIPT_NO_RE.search(text)
    if receipt_match:
        result.receipt_number = receipt_match.group(1)

    return result


def parse_receipt_pdf(data: bytes) -> ReceiptData:
    return parse_kaspi_receipt(extract_text_from_pdf(data))
