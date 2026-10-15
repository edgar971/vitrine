"""The ``mcp-drop`` command line (spec § 9).

It sits above every layer, like ``app.py``: it reads the configuration, builds what one command
needs and prints the result. Exit status: 0 on success, 1 when the command failed, 2 when the
configuration or the arguments are wrong.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn

from mcp_drop import __version__
from mcp_drop.app import STAGING_GRACE, build_app
from mcp_drop.config import (
    SECRET_KEYS,
    LogFormat,
    Settings,
    load_settings,
    parse_duration,
    parse_tokens,
    plain_http_warning,
    unscoped_tokens,
)
from mcp_drop.domain.clock import SystemClock
from mcp_drop.domain.errors import ConfigError, DropError
from mcp_drop.domain.models import Scope
from mcp_drop.services import maintenance
from mcp_drop.services.audit import AuditEvent, AuditLog
from mcp_drop.services.container import Services
from mcp_drop.services.tokens import token_hash
from mcp_drop.storage import schema
from mcp_drop.storage.backup import backup

if TYPE_CHECKING:
    from mcp_drop.metrics import PrometheusMetrics

PROG = "mcp-drop"
OK, FAILED, MISCONFIGURED = 0, 1, 2
REDACTED = "[redacted]"
TOKEN_BYTES = 32
SECRET_BYTES = 32
MISSING_EXTRA = "metrics_port needs the metrics extra: install mcp-drop[metrics]"
TEXT_LOG = "%(asctime)s %(levelname)s %(name)s: %(message)s"

logger = logging.getLogger(__name__)

type Command = Callable[[argparse.Namespace], int]


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the arguments, run one command and map a domain error to an exit status."""
    args = _parser().parse_args(argv)
    command: Command = args.command
    try:
        return command(args)
    except ConfigError as error:
        _fail(f"configuration: {error}")
        return MISCONFIGURED
    except DropError as error:
        _fail(str(error))
        return FAILED


def _serve(args: argparse.Namespace) -> int:
    """Run the server until interrupted."""
    flags = {key: value for key in ("host", "port") if (value := getattr(args, key)) is not None}
    settings = load_settings(config_path=args.config, flags=flags)
    _configure_logging(settings)
    if (warning := plain_http_warning(settings)) is not None:
        logger.warning(warning)
    # Refuse before anything is written: a build must never serve data it would misread.
    schema.require_current(settings.data_dir)
    metrics = _metrics(settings)
    services = Services.from_settings(settings, metrics=metrics)
    if metrics is not None:
        metrics.measure(services.artifacts.usage)
        metrics.serve(host=settings.host, port=settings.metrics_port)
    uvicorn.run(
        build_app(settings, services),
        host=settings.host,
        port=settings.port,
        # build_app logs (redacted) and reads X-Forwarded-For itself, from trusted proxies only.
        access_log=False,
        proxy_headers=False,
        server_header=False,
        log_config=None,
    )
    return OK


def _metrics(settings: Settings) -> PrometheusMetrics | None:
    """The Prometheus exporter when ``metrics_port`` is set; a missing extra stops startup."""
    if not settings.metrics_port:
        return None
    try:
        from mcp_drop.metrics import PrometheusMetrics  # noqa: PLC0415 - the optional extra
    except ImportError:
        raise ConfigError(MISSING_EXTRA) from None
    return PrometheusMetrics()


def _config_check(args: argparse.Namespace) -> int:
    """Validate the configuration and print the effective values, secrets redacted."""
    settings = load_settings(config_path=args.config)
    for key, value in settings.model_dump(mode="json").items():
        shown = REDACTED if key in SECRET_KEYS else value
        print(f"{key} = {json.dumps(shown)}")
    tokens = ", ".join(
        f"{entry.name} ({'+'.join(sorted(_short(scope) for scope in entry.scopes))})"
        for entry in settings.tokens.values()
    )
    print(f"# clients: {tokens}")
    for name in unscoped_tokens(os.environ.get("DROP_TOKENS", "")):
        print(
            f"warning: token {name!r} lists no scopes, so it holds all four; "
            "name them (e.g. :read+write)",
            file=sys.stderr,
        )
    if (warning := plain_http_warning(settings)) is not None:
        print(f"warning: {warning}", file=sys.stderr)
    print("configuration ok", file=sys.stderr)
    return OK


def _token_new(args: argparse.Namespace) -> int:
    """Print a new token and its ``DROP_TOKENS`` entry; nothing is written anywhere."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    scopes = "+".join(name.strip() for name in args.scopes.split(",") if name.strip())
    entry = f"{args.name}:{token_hash(token)}:{scopes}"
    parse_tokens(entry)  # the same rules the server applies to the name and the scopes
    print(f"token: {token}")
    print(f"entry: {entry}")
    print("Give the token to the client and add the entry to DROP_TOKENS.", file=sys.stderr)
    return OK


def _token_hash(args: argparse.Namespace) -> int:
    """Print the hash of an existing token."""
    print(token_hash(args.token))
    return OK


def _secret_new(_args: argparse.Namespace) -> int:
    """Print a new random secret for ``DROP_SECRET``."""
    print(secrets.token_hex(SECRET_BYTES))
    return OK


def _sweep(args: argparse.Namespace) -> int:
    """Run the expiry and staging sweep once, beside a server that may be running."""
    settings = load_settings(config_path=args.config)
    schema.require_current(settings.data_dir)
    report = Services.from_settings(settings).artifacts.sweep(grace=STAGING_GRACE)
    print(
        f"expired {report.expired}, staging {report.staging}, refs {report.refs}, "
        f"audit months {report.audit_months}"
    )
    return OK


def _audit(args: argparse.Namespace) -> int:
    """Print the events that match, newest first."""
    settings = load_settings(config_path=args.config)
    clock = SystemClock()
    since = clock.now() - parse_duration(args.since) if args.since else None
    log = AuditLog(settings.data_dir, retention=settings.audit_retention, clock=clock)
    for event in log.query(artifact=args.id, client=args.client, since=since):
        print(
            event.model_dump_json(by_alias=True, exclude_none=True) if args.json else _line(event)
        )
    return OK


def _line(event: AuditEvent) -> str:
    """One event as a readable line: time, event, artifact, client, then the extras."""
    extras = event.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
        exclude={"ts", "event", "artifact", "client"},
    )
    detail = " ".join(f"{key}={value}" for key, value in extras.items())
    stamp = event.ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"{stamp}  {event.event:<18} {event.artifact or '-':<16}  {event.client}  {detail}".rstrip()
    )


def _backup(args: argparse.Namespace) -> int:
    """Copy ``data_dir`` into a new directory that can be restored as-is."""
    settings = load_settings(config_path=args.config)
    report = backup(settings.data_dir, args.dest)
    print(f"artifacts {report.artifacts}, versions {report.versions}, bytes {report.bytes}")
    return OK


def _migrate(args: argparse.Namespace) -> int:
    """Upgrade older ``meta.json`` files, or show which would change."""
    settings = load_settings(config_path=args.config)
    clock = SystemClock()
    log = AuditLog(settings.data_dir, retention=settings.audit_retention, clock=clock)
    changed = maintenance.migrate(settings.data_dir, audit=log, clock=clock, dry_run=args.dry_run)
    for artifact_id in changed:
        print(artifact_id)
    verb = "would migrate" if args.dry_run else "migrated"
    print(f"{verb} {len(changed)} artifact(s)", file=sys.stderr)
    return OK


def _configure_logging(settings: Settings) -> None:
    """One handler on the root logger, in the configured level and format (spec § 8.2)."""
    handler = logging.StreamHandler()
    if settings.log_format is LogFormat.JSON:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(TEXT_LOG))
    logging.basicConfig(level=settings.log_level.value, handlers=[handler], force=True)


class _JsonFormatter(logging.Formatter):
    """One JSON object per line, for log shippers."""

    def format(self, record: logging.LogRecord) -> str:
        """Time, level, logger and message; an exception's traceback when there is one."""
        line = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line)


def _short(scope: Scope) -> str:
    """A scope as ``DROP_TOKENS`` spells it: ``read``, not ``drop:read``."""
    return scope.value.removeprefix("drop:")


def _fail(message: str) -> None:
    """Report one failure on stderr."""
    print(f"{PROG}: {message}", file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    """The commands of spec § 9."""
    parser = argparse.ArgumentParser(
        prog=PROG, description="Private and public links for agent output."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(title="commands", required=True, metavar="COMMAND")

    def command(name: str, run: Command, summary: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=summary, description=summary)
        sub.set_defaults(command=run)
        return sub

    def config_flag(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--config", type=Path, metavar="FILE", help="a drop.toml")

    serve = command("serve", _serve, "Run the server.")
    config_flag(serve)
    serve.add_argument("--host", metavar="H", help="listen address (overrides host)")
    serve.add_argument("--port", type=int, metavar="P", help="listen port (overrides port)")

    config = commands.add_parser("config", help="Inspect the configuration.")
    config_commands = config.add_subparsers(title="commands", required=True, metavar="COMMAND")
    check = config_commands.add_parser(
        "check", help="Validate it and print the effective values, secrets redacted."
    )
    check.set_defaults(command=_config_check)
    config_flag(check)

    token = commands.add_parser("token", help="Create client tokens.")
    token_commands = token.add_subparsers(title="commands", required=True, metavar="COMMAND")
    new_token = token_commands.add_parser("new", help="Print a new token and its entry.")
    new_token.set_defaults(command=_token_new)
    new_token.add_argument("name", help="the client's name, recorded as each artifact's source")
    new_token.add_argument(
        "--scopes",
        default=",".join(_short(scope) for scope in Scope),
        help="comma-separated: read, write, share, publish (default: all four)",
    )
    hash_token = token_commands.add_parser("hash", help="Print the hash of an existing token.")
    hash_token.set_defaults(command=_token_hash)
    hash_token.add_argument("token")

    secret = commands.add_parser("secret", help="Create the signing secret.")
    secret_commands = secret.add_subparsers(title="commands", required=True, metavar="COMMAND")
    secret_commands.add_parser("new", help="Print a new random secret.").set_defaults(
        command=_secret_new
    )

    config_flag(command("sweep", _sweep, "Run the expiry and staging sweep once."))

    audit = command("audit", _audit, "Print audit events, newest first.")
    config_flag(audit)
    audit.add_argument("--id", help="only this artifact")
    audit.add_argument("--client", help="only this client")
    audit.add_argument("--since", metavar="AGE", help="only events this recent, e.g. 7d or 24h")
    audit.add_argument("--json", action="store_true", help="one JSON object per line")

    backup_command = command("backup", _backup, "Write a restorable copy of data_dir.")
    backup_command.add_argument("dest", type=Path, help="a new or empty directory")
    config_flag(backup_command)

    migrate = command("migrate", _migrate, "Upgrade meta.json files to the current schema.")
    migrate.add_argument("--dry-run", action="store_true", help="show what would change")
    config_flag(migrate)
    return parser
