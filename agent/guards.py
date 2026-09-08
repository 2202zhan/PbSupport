"""Checks on what the agent wrote, enforced in code rather than by asking nicely.

Two of the rules the menu bot kept breaking are the ones a prompt is worst at
holding: never claim a hand-off that isn't happening, and never tell someone
standing at our kiosk to service it. Both are checked here, on the text itself,
so a model that drifts is corrected instead of believed.
"""

import re

from ai_decider import promises_a_handoff

__all__ = ["promises_a_handoff", "self_service_advice"]

# Told to the user, in the second person: "перезагрузите киоск", "замените
# картридж". Our own first-person plans ("заменим картридж", "перезагрузим
# аппарат") are fine - that is us describing our work, not an instruction to a
# student standing in a corridor.
_SELF_SERVICE = [
    re.compile(
        r"(?:перезагруз|перезапуст|выключ|включ|переверн)(?:ите|и|ить|ать|уть)?\b[^.!?\n]{0,25}"
        r"(?:аппарат|киоск|принтер|терминал|экран|устройств)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:замен|встряхн|вытащ|достан)(?:ите|и|ить|уть)?\b[^.!?\n]{0,25}(?:картридж|тонер)", re.IGNORECASE),
    re.compile(r"(?:почист|протр|продув)(?:ите|и|ить|ать)?\b", re.IGNORECASE),
    re.compile(r"(?:досып|подсып|полож|заряд|вставь|вставьте)(?:ьте|ите|ать|ить)?\b[^.!?\n]{0,25}бумаг", re.IGNORECASE),
    re.compile(r"(?:откр|сним)(?:ойте|ыть|ите|ать)\b[^.!?\n]{0,25}(?:крышк|лоток|панел|аппарат)", re.IGNORECASE),
]

# "Перезагрузите телефон/телеграм" is fine - that is the user's own device.
_NOT_OURS = re.compile(r"телефон|телеграм|telegram|приложени|браузер", re.IGNORECASE)


def self_service_advice(text: str | None) -> str | None:
    """The offending phrase, or None. The kiosk is ours, closed and serviced by
    us; telling its user to open, restart or refill it is at best useless and
    at worst insulting to someone who has just lost 70 ₸."""
    if not text:
        return None
    for pattern in _SELF_SERVICE:
        match = pattern.search(text)
        if match and not _NOT_OURS.search(match.group(0)):
            return match.group(0)
    return None
