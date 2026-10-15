"""Browser givens: the HTTP specs' server, reused as is."""

from __future__ import annotations

from tests.http.conftest import (  # noqa: F401 - fixtures, registered by being imported here
    base,
    overrides,
    port,
    services,
    settings,
)
