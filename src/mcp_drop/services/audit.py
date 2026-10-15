"""``AuditLog``: the append-only record of every state change (spec § 11.3)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from mcp_drop.domain import layout
from mcp_drop.domain.clock import Clock
from mcp_drop.domain.errors import AuditUnavailable, SchemaMismatch
from mcp_drop.domain.models import GateDecision, Visibility


class AuditEventName(StrEnum):
    """Every event the log holds (spec § 11.3)."""

    CREATED = "created"
    VERSION_ADDED = "version_added"
    VISIBILITY_CHANGED = "visibility_changed"
    PUBLISH_REFUSED = "publish_refused"
    LINKS_REVOKED = "links_revoked"
    UPLOAD_URL_ISSUED = "upload_url_issued"
    DELETED = "deleted"
    EXPIRED = "expired"
    ACCESS_DENIED = "access_denied"


class DenialReason(StrEnum):
    """Why a call was refused without doing anything (spec § 11.3)."""

    SCOPE = "scope"
    RATE_LIMITED = "rate_limited"


EXTRA_FIELDS = (
    "version",
    "files",
    "bytes",
    "client_ref",
    "merge",
    "gate",
    "from_visibility",
    "to_visibility",
    "link_epoch",
    "expires_at",
    "reason",
    "tool",
    "route",
)

REQUIRED_EXTRAS: dict[AuditEventName, frozenset[str]] = {
    AuditEventName.CREATED: frozenset({"version", "files", "bytes"}),
    AuditEventName.VERSION_ADDED: frozenset({"version", "files", "bytes", "merge"}),
    AuditEventName.VISIBILITY_CHANGED: frozenset({"from_visibility", "to_visibility", "gate"}),
    AuditEventName.PUBLISH_REFUSED: frozenset({"gate"}),
    AuditEventName.LINKS_REVOKED: frozenset({"link_epoch"}),
    AuditEventName.UPLOAD_URL_ISSUED: frozenset({"expires_at"}),
    AuditEventName.DELETED: frozenset(),
    AuditEventName.EXPIRED: frozenset(),
    AuditEventName.ACCESS_DENIED: frozenset({"reason"}),
}

OPTIONAL_EXTRAS: dict[AuditEventName, frozenset[str]] = {
    AuditEventName.CREATED: frozenset({"client_ref"})
}


class AuditEvent(BaseModel):
    """One line of ``audit/<YYYY-MM>.jsonl``: who did what to which artifact, never any content.

    The model is the contract: every field is one the spec documents, and nothing else fits, so a
    title, a file name, a signature or a token cannot end up in the log by accident.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    ts: AwareDatetime
    event: AuditEventName
    artifact: str
    client: str
    version: int | None = None
    files: int | None = None
    bytes: int | None = None
    client_ref: str | None = None
    merge: bool | None = None
    gate: GateDecision | None = None
    from_visibility: Visibility | None = Field(default=None, alias="from")
    to_visibility: Visibility | None = Field(default=None, alias="to")
    link_epoch: int | None = None
    expires_at: AwareDatetime | None = None
    reason: DenialReason | None = None
    tool: str | None = None
    route: str | None = None

    @model_validator(mode="after")
    def _carries_the_documented_fields(self) -> AuditEvent:
        """Require the extras spec § 11.3 lists for this event, and no others."""
        carried = {name for name in EXTRA_FIELDS if getattr(self, name) is not None}
        required = REQUIRED_EXTRAS[self.event]
        if not required <= carried:
            raise ValueError(f"{self.event} requires {sorted(required - carried)}")
        unexpected = carried - required - OPTIONAL_EXTRAS.get(self.event, frozenset())
        if unexpected:
            raise ValueError(f"{self.event} does not carry {sorted(unexpected)}")
        if self.event is AuditEventName.ACCESS_DENIED and (self.tool is None) == (
            self.route is None
        ):
            raise ValueError("access_denied names exactly one of tool or route")
        return self


class AuditLog:
    """Appends one JSON object per line, fsync'd before the change is acknowledged."""

    def __init__(self, root: Path, *, retention: timedelta, clock: Clock) -> None:
        """Write ``root/audit/<YYYY-MM>.jsonl`` and keep each month for ``retention``."""
        self._data_dir = root
        self._root = root / layout.AUDIT_DIR
        self._retention = retention
        self._clock = clock
        self._root.mkdir(parents=True, exist_ok=True)

    def append(self, event: AuditEvent) -> None:
        """Append one event and flush it to disk, or raise ``AuditUnavailable`` (spec § 11.3)."""
        line = event.model_dump_json(by_alias=True, exclude_none=True)
        path = layout.audit_file(self._data_dir, event.ts)
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{line}\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            raise AuditUnavailable(
                f"cannot audit {event.event}: {error.strerror or error}"
            ) from error

    def query(
        self,
        *,
        artifact: str | None = None,
        client: str | None = None,
        since: datetime | None = None,
    ) -> tuple[AuditEvent, ...]:
        """Read the events that match, newest first (spec § 5.2, § 9)."""
        found = [
            event
            for path in sorted(self._root.glob("*.jsonl"), reverse=True)
            for event in self._read_month(path)
            if _matches(event, artifact=artifact, client=client, since=since)
        ]
        found.sort(key=lambda event: event.ts, reverse=True)
        return tuple(found)

    def sweep(self) -> int:
        """Delete whole months older than the retention window (spec § 11.3)."""
        cutoff = self._clock.now() - self._retention
        removed = 0
        for path in sorted(self._root.glob("*.jsonl")):
            month = _month_of(path)
            if month is not None and month < (cutoff.year, cutoff.month):
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def _read_month(self, path: Path) -> list[AuditEvent]:
        """Parse one month's lines, newest last-then-reversed, refusing anything unreadable."""
        events = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                events.append(AuditEvent.model_validate_json(line))
            except ValidationError as error:
                raise SchemaMismatch(
                    f"{path.name} line {number} is not an audit event: {error}"
                ) from error
        events.reverse()
        return events


def _matches(
    event: AuditEvent, *, artifact: str | None, client: str | None, since: datetime | None
) -> bool:
    """Whether one event satisfies every filter."""
    if artifact is not None and event.artifact != artifact:
        return False
    if client is not None and event.client != client:
        return False
    return since is None or event.ts >= since


def _month_of(path: Path) -> tuple[int, int] | None:
    """The (year, month) a log file name names, or ``None`` when it is not one of ours."""
    try:
        month = datetime.strptime(path.stem, "%Y-%m")
    except ValueError:
        return None
    return month.year, month.month
