"""Getting a receipt out of a Telegram message.

Shared by the menu bot and the agent: a receipt has to mean the same thing
whichever door it came through. receipt_parser does the reading; this module
decides what a message contains and whether what it says is still usable.
"""

import logging
from datetime import datetime, timedelta

import receipt_parser
import tz

logger = logging.getLogger(__name__)

# Device logs and printer history don't reach back further than this, so a
# complaint older than a day cannot be checked at all - and the receipt is the
# one thing that can reveal the real date when the user's own answer didn't.
MAX_AGE = timedelta(hours=24)


def is_stale(moment: datetime | None) -> bool:
    return moment is not None and tz.now() - moment > MAX_AGE


async def extract(bot, message) -> tuple[str, bool, receipt_parser.ReceiptData | None]:
    """Returns (file_id, is_document, parsed).

    Only PDFs are parsed - photos are accepted so a person can look at them
    later, but there is no OCR here and pretending otherwise would be worse
    than saying so.
    """
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
