"""Shapes shared by the runtime, the tools and the router.

Kept apart from both so a tool can describe what it wants done without
importing the loop that will do it.
"""

from dataclasses import dataclass, field


@dataclass
class TurnContext:
    """Everything a tool may need to know about who it is answering."""

    telegram_id: str
    username: str | None
    conversation_id: int
    user_message: str


@dataclass
class TurnResult:
    """What the turn decided. The runtime never touches Telegram itself - it
    returns this, and the router carries it out. That keeps the loop testable
    without a Bot and keeps every side effect in one place."""

    kind: str  # "reply" | "escalate" | "failed"
    text: str | None = None
    staff_summary: str | None = None
    reason: str | None = None
    tool_calls: list[str] = field(default_factory=list)
    model_calls: int = 0
    error: str | None = None

    @property
    def needs_staff(self) -> bool:
        return self.kind in ("escalate", "failed")
