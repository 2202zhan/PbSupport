"""The conversational agent that replaces the menu tree.

Being built phase by phase behind settings.agent_mode - see PLAN.md. Every user
the gate does not admit keeps going through triage.py exactly as before.

Only the gate is re-exported here: binding the Router object as `agent.router`
would shadow the `agent.router` module, and then importing it gets you the
object instead of the module.
"""

from agent.gate import agent_enabled_for, describe_mode

__all__ = ["agent_enabled_for", "describe_mode"]
