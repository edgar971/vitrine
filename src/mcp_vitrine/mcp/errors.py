"""Domain errors become MCP errors here, once, for every tool and resource (spec § 10).

It wraps the function rather than sitting in middleware because FastMCP masks an unknown exception
before middleware sees it; a ``VitrineError`` must reach the client as its own message, which is
already worded so that it never says whether a private artifact exists.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import cast

from fastmcp.exceptions import ResourceError, ToolError

from mcp_vitrine.domain.errors import (
    Conflict,
    Expired,
    Invalid,
    NotFound,
    OverQuota,
    RateLimited,
    Refused,
    TooLarge,
    VitrineError,
)

INTERNAL = "internal error"
# The errors worded for the caller, the same set HTTP gives a 4xx. Anything else is a failure of
# vitrine itself whose text can carry filesystem or parser detail, so it is masked (spec § 10).
_CALLER_ERRORS = (NotFound, Expired, Invalid, Conflict, TooLarge, RateLimited, OverQuota, Refused)

logger = logging.getLogger(__name__)


def domain_errors[**P, R](
    error: type[ToolError] | type[ResourceError],
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Re-raise a ``VitrineError`` as ``error`` with the same message and no logged traceback."""

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        if inspect.iscoroutinefunction(function):
            coroutine = cast(Callable[P, Awaitable[object]], function)

            @functools.wraps(function)
            async def awaited(*args: P.args, **kwargs: P.kwargs) -> object:
                try:
                    return await coroutine(*args, **kwargs)
                except VitrineError as failure:
                    raise _translated(error, failure) from None

            return cast(Callable[P, R], awaited)

        @functools.wraps(function)
        def called(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return function(*args, **kwargs)
            except VitrineError as failure:
                raise _translated(error, failure) from None

        return called

    return decorate


def _translated(
    error: type[ToolError] | type[ResourceError], failure: VitrineError
) -> ToolError | ResourceError:
    """The client-facing error for ``failure``; an internal one is logged here and masked."""
    if not isinstance(failure, _CALLER_ERRORS):
        logger.error("internal error: %s", failure)
        return error(INTERNAL)
    return error(str(failure), log_level=logging.INFO)
