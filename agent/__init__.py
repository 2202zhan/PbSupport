"""The conversational agent that replaces the menu tree.

Being built phase by phase behind settings.agent_mode - see PLAN.md. Until it
is complete, every user whom the gate does not admit keeps going through
triage.py exactly as before.
"""

from agent.gate import agent_enabled_for, describe_mode
from agent.router import router

__all__ = ["agent_enabled_for", "describe_mode", "router"]
