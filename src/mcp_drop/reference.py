"""``docs/configuration.md``, generated from ``Settings`` so the reference cannot drift.

``just docs`` rewrites the file; a spec fails when the committed copy differs from this output.
"""

from __future__ import annotations

from enum import Enum

from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from mcp_drop.config import (
    ENV_PREFIX,
    GROUP,
    LIMITS,
    LINKS,
    OPERATIONS,
    POLICY,
    SECRETS,
    SERVING,
    Settings,
)

GROUPS = (SERVING, SECRETS, POLICY, LIMITS, LINKS, OPERATIONS)

HEADER = """\
# drop configuration

<!-- Generated from mcp_drop.config.Settings by `just docs`. Do not edit by hand. -->

Every key can be set as `DROP_<KEY>` in the environment or in a TOML file passed with `--config`
(top-level keys or a `[drop]` table); `--host` and `--port` override both. The secrets are read
from the environment only, and a file that sets one is refused. Unknown keys are refused too.
The contract, including the accepted formats, is [spec § 8](spec.md#8-configuration).

| Kind | Accepted forms |
|---|---|
| Sizes | `512KB`, `100MB`, `10GB` or bytes (binary units) |
| Durations | `30s`, `15m`, `1h`, `7d` or seconds |
| Lists | Comma-separated in the environment; arrays in TOML |
| Rates | `<int>/<s\\|m\\|h>`, for example `60/m`, or `off` |
"""


def render() -> str:
    """The whole reference, one table per group, in declaration order."""
    sections = [HEADER]
    for group in GROUPS:
        rows = [
            f"| `{name}` | `{ENV_PREFIX}{name.upper()}` | {_default(field)} | {field.description} |"
            for name, field in Settings.model_fields.items()
            if group_of(field) == group
        ]
        sections.append(
            "\n".join(
                [
                    f"## {group}",
                    "",
                    "| Key | Environment | Default | Description |",
                    "|---|---|---|---|",
                    *rows,
                ]
            )
            + "\n"
        )
    return "\n".join(sections)


def group_of(field: FieldInfo) -> str | None:
    """The group a field declares, if any."""
    extra = field.json_schema_extra
    group = extra.get(GROUP) if isinstance(extra, dict) else None
    return group if isinstance(group, str) else None


def _default(field: FieldInfo) -> str:
    """A default as an operator would write it."""
    value = field.default
    if value is PydanticUndefined:
        return "required"
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, list):
        return "`[" + ", ".join(f'"{item}"' for item in value) + "]`"
    return f"`{value}`"


if __name__ == "__main__":
    print(render(), end="")
