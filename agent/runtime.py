"""One turn of the agent.

The loop is: ask the model, run whatever tools it called, ask again with the
results, until a terminal tool ends the turn. Everything about it is bounded -
model calls, tool calls, wall-clock - and every way out ends in something the
user can see. A turn that runs out of budget, times out or throws hands the
case to a human; it never ends in silence.
"""

import asyncio
import json
import logging
import time

from openai import AsyncOpenAI

import openai_utils
import storage
from agent import memory
from agent.prompt import SYSTEM_PROMPT
from agent.tools import DEFAULT_TOOLS, ToolError, ToolRegistry
from agent.types import TurnContext, TurnResult
from config import settings

logger = logging.getLogger(__name__)

_STUCK_SUMMARY = (
    "Агент не смог довести разговор до ответа (исчерпан бюджет хода или ошибка модели) — "
    "нужен живой сотрудник."
)
_STUCK_MESSAGE = "Тут мне нужен коллега — передал ваше обращение сотруднику, он ответит здесь."


def _client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=settings.openai_api_key)


def _stuck(error: str, tool_calls: list[str], model_calls: int) -> TurnResult:
    return TurnResult(
        kind="failed",
        text=_STUCK_MESSAGE,
        staff_summary=_STUCK_SUMMARY,
        reason="agent_stuck",
        tool_calls=tool_calls,
        model_calls=model_calls,
        error=error,
    )


async def run_turn(ctx: TurnContext, tools: ToolRegistry = DEFAULT_TOOLS) -> TurnResult:
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(
            _run(ctx, tools), timeout=settings.agent_turn_timeout_seconds
        )
    except asyncio.TimeoutError:
        logger.warning("agent turn timed out for %s", ctx.telegram_id)
        result = _stuck("timeout", [], 0)
    except Exception as exc:
        logger.exception("agent turn failed for %s", ctx.telegram_id)
        result = _stuck(f"{type(exc).__name__}: {exc}", [], 0)

    await storage.record_agent_turn(
        telegram_id=ctx.telegram_id,
        conversation_id=ctx.conversation_id,
        outcome=result.kind,
        user_message=ctx.user_message,
        reply=result.text,
        tool_calls=result.tool_calls,
        error=result.error,
        model_calls=result.model_calls,
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    return result


async def _run(ctx: TurnContext, tools: ToolRegistry) -> TurnResult:
    called: list[str] = []
    model_calls = 0

    for _ in range(settings.agent_max_model_calls):
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *await memory.history(ctx.conversation_id)]
        response = await openai_utils.call_with_one_retry(
            lambda m=messages: _client().chat.completions.create(
                model=settings.agent_chat_model,
                messages=m,
                tools=tools.schemas,
                tool_choice="required",
            )
        )
        model_calls += 1
        message = response.choices[0].message

        if not message.tool_calls:
            # Asked for a tool and got prose. It is still an answer to the
            # user, so deliver it rather than making them wait for a retry.
            text = (message.content or "").strip()
            if text:
                await memory.remember_assistant_message(ctx.conversation_id, text)
                return TurnResult(kind="reply", text=text, tool_calls=called, model_calls=model_calls)
            return _stuck("model returned neither a tool call nor text", called, model_calls)

        await memory.remember_assistant_message(
            ctx.conversation_id,
            message.content,
            tool_calls=[c.model_dump() for c in message.tool_calls],
        )

        for call in message.tool_calls:
            name = call.function.name
            called.append(name)
            if len(called) > settings.agent_max_tool_calls:
                return _stuck("tool budget exhausted", called, model_calls)

            spec = tools.get(name)
            if spec is None:
                await memory.remember_tool_result(
                    ctx.conversation_id, name, call.id, f"нет такого инструмента: {name}"
                )
                continue

            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                await memory.remember_tool_result(
                    ctx.conversation_id, name, call.id, "аргументы не разобрались как JSON"
                )
                continue

            try:
                outcome = await spec.run(args, ctx)
            except ToolError as exc:
                # Recoverable: tell the model what it got wrong and let it try
                # again inside the same budget.
                await memory.remember_tool_result(ctx.conversation_id, name, call.id, str(exc))
                continue

            if spec.terminal:
                await memory.remember_tool_result(ctx.conversation_id, name, call.id, "ok")
                outcome.tool_calls = called
                outcome.model_calls = model_calls
                return outcome

            await memory.remember_tool_result(ctx.conversation_id, name, call.id, outcome)

    return _stuck("model call budget exhausted", called, model_calls)
