"""Who gets the agent instead of the menu tree."""

import logging

from config import settings

logger = logging.getLogger(__name__)

OFF, ADMIN, ALL = "off", "admin", "all"
_MODES = (OFF, ADMIN, ALL)


def _mode() -> str:
    raw = (settings.agent_mode or OFF).strip().lower()
    if raw not in _MODES:
        # Fail towards the path that is known to work: a typo in the flag must
        # not silently route real users into a half-built agent.
        logger.warning("unknown agent_mode %r - falling back to %r", settings.agent_mode, OFF)
        return OFF
    return raw


def _admin_ids() -> set[str]:
    return {x.strip() for x in settings.admin_telegram_ids.split(",") if x.strip()}


def agent_enabled_for(telegram_id: str | int | None) -> bool:
    mode = _mode()
    if mode == ALL:
        return True
    if mode == ADMIN:
        return str(telegram_id) in _admin_ids()
    return False


def describe_mode() -> str:
    """One line for the startup log, so it is obvious from the console which
    path real users are on."""
    mode = _mode()
    if mode == ALL:
        return "agent: включён для всех"
    if mode == ADMIN:
        admins = ", ".join(sorted(_admin_ids())) or "нет админов в списке"
        return f"agent: только для админов ({admins})"
    return "agent: выключен, все идут через меню"
