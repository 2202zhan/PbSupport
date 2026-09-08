"""How a tool is described and looked up.

Two kinds. A data tool answers the model and the loop continues. A terminal
tool ends the turn: it returns a TurnResult and nothing further is asked of the
model.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from agent.types import TurnContext


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    terminal: bool
    run: Callable[[dict[str, Any], TurnContext], Awaitable[Any]]

    @property
    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolError(Exception):
    """The model called a tool wrongly. Reported back to it as a tool result so
    it can correct itself inside the turn's budget, rather than failing the
    whole turn over a missing argument."""


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec]) -> None:
        self._specs = {s.name: s for s in specs}

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [s.schema for s in self._specs.values()]

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)


MAX_BUTTONS = 4
MAX_BUTTON_LABEL = 40
EXPECTATIONS = ("text", "choice", "file", "none")


def sanitize_buttons(raw: Any) -> list[str]:
    """Buttons are a convenience, so a malformed one is dropped rather than
    failing the turn - the text of the message still stands on its own."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ToolError("buttons должен быть списком коротких подписей")
    labels: list[str] = []
    for item in raw:
        label = item.get("label") if isinstance(item, dict) else item
        if not isinstance(label, str):
            continue
        label = " ".join(label.split())[:MAX_BUTTON_LABEL]
        if label and label not in labels:
            labels.append(label)
    return labels[:MAX_BUTTONS]


