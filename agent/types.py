"""Shapes shared by the runtime, the tools and the router.

Kept apart from both so a tool can describe what it wants done without
importing the loop that will do it.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TurnContext:
    """Everything a tool may need to know about who it is answering."""

    telegram_id: str
    username: str | None
    conversation_id: int
    user_message: str
    api: Any = None
    # Called by a tool that takes seconds, so the user sees the bot working
    # instead of a silent gap. Optional: the runtime is testable without one.
    on_progress: Any = None
    # Where the reading tools put the figures. The model never sees this - it
    # goes on the escalation card, where a person can use it. That is what keeps
    # toner percentages and sheet counts out of the user's reply structurally,
    # instead of by asking the model not to mention them.
    staff_notes: list[str] = field(default_factory=list)


@dataclass
class TurnResult:
    """What the turn decided. The runtime never touches Telegram itself - it
    returns this, and the router carries it out. That keeps the loop testable
    without a Bot and keeps every side effect in one place."""

    kind: str  # "reply" | "escalate" | "failed"
    text: str | None = None
    # Quick answers drawn for this particular message. Never a gate: whatever
    # is on them, the user can always just type instead.
    buttons: list[str] = field(default_factory=list)
    # What the agent is waiting for now: "text", "choice", "file" or "none".
    expect: str = "text"
    staff_summary: str | None = None
    reason: str | None = None
    tool_calls: list[str] = field(default_factory=list)
    model_calls: int = 0
    error: str | None = None

    @property
    def needs_staff(self) -> bool:
        return self.kind in ("escalate", "failed")
