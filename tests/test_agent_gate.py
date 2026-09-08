"""Phase 0: the switch between the menu bot and the agent.

Nothing hangs off this yet - the point is that the gate, the flag and the
bookkeeping are proven before any behaviour depends on them, and that an
unreadable flag can never hand a real user to a half-built path.
"""
import agent
from agent import gate


def _flag(monkeypatch, mode, admins="884013433,111"):
    monkeypatch.setattr(gate.settings, "agent_mode", mode)
    monkeypatch.setattr(gate.settings, "admin_telegram_ids", admins)


def test_off_sends_everyone_through_the_menu(monkeypatch):
    _flag(monkeypatch, "off")
    assert gate.agent_enabled_for("884013433") is False
    assert gate.agent_enabled_for("999") is False


def test_admin_mode_admits_only_the_listed_ids(monkeypatch):
    _flag(monkeypatch, "admin")
    assert gate.agent_enabled_for("884013433") is True
    assert gate.agent_enabled_for(884013433) is True  # int ids arrive from aiogram
    assert gate.agent_enabled_for("999") is False


def test_all_admits_everyone(monkeypatch):
    _flag(monkeypatch, "all")
    assert gate.agent_enabled_for("999") is True


def test_an_unreadable_flag_falls_back_to_the_menu(monkeypatch):
    # A typo in the environment must not route real users into an unfinished
    # agent - the failure direction is towards the path that works.
    _flag(monkeypatch, "ADMINS")
    assert gate.agent_enabled_for("884013433") is False
    _flag(monkeypatch, "")
    assert gate.agent_enabled_for("884013433") is False


def test_case_and_spaces_in_the_flag_are_tolerated(monkeypatch):
    _flag(monkeypatch, "  Admin ")
    assert gate.agent_enabled_for("884013433") is True


def test_admin_mode_with_no_admins_admits_nobody(monkeypatch):
    _flag(monkeypatch, "admin", admins="")
    assert gate.agent_enabled_for("884013433") is False


def test_the_startup_line_names_the_active_path(monkeypatch):
    # This line is what tells you from the console which path live users are
    # on, so it has to distinguish all three states.
    _flag(monkeypatch, "off")
    assert "выключен" in agent.describe_mode()
    _flag(monkeypatch, "admin")
    assert "884013433" in agent.describe_mode()
    _flag(monkeypatch, "all")
    assert "для всех" in agent.describe_mode()


def test_the_agent_router_is_registered_before_triage():
    # Order is the whole mechanism: admitted users must be handled by the agent
    # and never reach the menu tree.
    import bot as bot_module
    import inspect

    source = inspect.getsource(bot_module.main)
    assert source.index("include_router(agent.router)") < source.index("include_router(triage.router)")


def test_the_router_only_admits_users_the_gate_allows(monkeypatch):
    from types import SimpleNamespace

    from agent.router import from_admitted_user

    _flag(monkeypatch, "admin")
    assert from_admitted_user(SimpleNamespace(from_user=SimpleNamespace(id=884013433))) is True
    assert from_admitted_user(SimpleNamespace(from_user=SimpleNamespace(id=999))) is False
    # A channel post has no from_user; it must not crash the filter.
    assert from_admitted_user(SimpleNamespace(from_user=None)) is False
