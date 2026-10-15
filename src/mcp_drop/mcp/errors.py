"""Domain errors become MCP errors here, once, for every tool and resource (spec § 10).

It wraps the function rather than sitting in middleware because FastMCP masks an unknown exception
before middleware sees it; a ``DropError`` must reach the client as its own message, which is
already worded so that it never says whether a private artifact exists.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import cast

from fastmcp.exceptions import ResourceError, ToolError

from mcp_drop.domain.errors import DropError


def domain_errors[**P, R](
    error: type[ToolError] | type[ResourceError],
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Re-raise a ``DropError`` as ``error`` with the same message, and no traceback in the log."""

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        if inspect.iscoroutinefunction(function):
            coroutine = cast(Callable[P, Awaitable[object]], function)

            @functools.wraps(function)
            async def awaited(*args: P.args, **kwargs: P.kwargs) -> object:
                try:
                    return await coroutine(*args, **kwargs)
                except DropError as failure:
                    raise error(str(failure), log_level=logging.INFO) from None

            return cast(Callable[P, R], awaited)

        @functools.wraps(function)
        def called(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return function(*args, **kwargs)
            except DropError as failure:
                raise error(str(failure), log_level=logging.INFO) from None

        return called

    return decorate
