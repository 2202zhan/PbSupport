"""Shared retry helper for OpenAI calls. ai_decider.py and concierge.py hit the
same transient failure modes (network blip, rate limit, provider-side 5xx) -
only those are worth a single retry; anything else (bad key, bad request) will
fail identically on a second try, so there's no point delaying the fallback.
"""

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError

logger = logging.getLogger(__name__)

_RETRYABLE_ERRORS = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)
_RETRY_DELAY_SECONDS = 1.5

T = TypeVar("T")


async def call_with_one_retry(make_call: Callable[[], Awaitable[T]]) -> T:
    """`make_call` is a zero-arg async callable performing the API request.
    Retries exactly once, only for transient errors, after a short backoff. A
    second failure (transient or not) propagates to the caller as-is."""
    try:
        return await make_call()
    except _RETRYABLE_ERRORS as exc:
        logger.warning("OpenAI call failed transiently, retrying once: %s", exc)
        await asyncio.sleep(_RETRY_DELAY_SECONDS)
        return await make_call()
