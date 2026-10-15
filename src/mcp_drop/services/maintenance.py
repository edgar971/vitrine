"""``migrate``: the audited upgrade of a whole ``data_dir`` (operations § 4)."""

from __future__ import annotations

from pathlib import Path

from mcp_drop.domain.clock import Clock
from mcp_drop.services.audit import OPERATOR, AuditEvent, AuditEventName, AuditLog
from mcp_drop.storage import schema


def migrate(
    root: Path,
    *,
    audit: AuditLog,
    clock: Clock,
    dry_run: bool = False,
    plan: schema.Plan | None = None,
) -> tuple[str, ...]:
    """Upgrade older ``meta.json`` files; the ids that changed, or would on a dry run.

    The ``migrated`` event is written before any file changes, so a run that cannot be audited
    changes nothing (spec § 11.3). A run with nothing to do writes no event.
    """
    plan = plan or schema.CURRENT
    pending = schema.migrate(root, dry_run=True, plan=plan)
    if dry_run or not pending:
        return pending
    audit.append(
        AuditEvent(
            ts=clock.now(),
            event=AuditEventName.MIGRATED,
            client=OPERATOR,
            version=plan.target,
            files=len(pending),
        )
    )
    return schema.migrate(root, plan=plan)
