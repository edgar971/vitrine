"""The ``vitrine`` command line: publish, list, link and revoke over ``/api`` (spec § 9).

The URL alone goes to stdout and every message to stderr, so ``url=$(vitrine publish r.html)``
works. Exit status: 0 on success, 1 when the server refused or could not be reached, 2 when the
arguments or the configuration are wrong.
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn

from mcp_vitrine import __version__
from mcp_vitrine.client.api import (
    ClientError,
    ConfigurationError,
    Endpoint,
    Json,
    collect,
    connected,
)

PROG = "vitrine"
OK, FAILED, MISCONFIGURED = 0, 1, 2
ARGV_REFUSAL = (
    "a token is never accepted as an argument, where every user can read it with ps: "
    "set VITRINE_TOKEN or pass --token-file"
)
PAGE_SIZE = 100
GENERATE = "generate"

type Command = Callable[[argparse.Namespace], int]


class _Parser(argparse.ArgumentParser):
    """argparse, with usage errors kept to exit status 2 and stderr, as everywhere else."""

    def error(self, message: str) -> NoReturn:
        """Print the usage error and exit 2."""
        self.print_usage(sys.stderr)
        self.exit(MISCONFIGURED, f"{self.prog}: {message}\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the arguments, run one command, and map a failure to its exit status."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(argument == "--token" or argument.startswith("--token=") for argument in arguments):
        _say(ARGV_REFUSAL)
        return MISCONFIGURED
    args = _parser().parse_args(arguments)
    command: Command = args.command
    try:
        return command(args)
    except ConfigurationError as error:
        _say(f"configuration: {error}")
        return MISCONFIGURED
    except ClientError as error:
        _say(str(error))
        return FAILED


def _publish(args: argparse.Namespace) -> int:
    """Upload files and folders as one private artifact, or as a new version of one."""
    if args.title is None and args.update is None:
        args.title = Path(args.paths[0]).resolve().name
    if args.ref is not None and args.update is not None:
        raise ConfigurationError("--ref applies to a new artifact, not --update")
    fields = [
        (name, value)
        for name, value in (
            ("title", args.title),
            ("entry", args.entry),
            ("project", args.project),
            ("expires_in", args.expires),
            ("note", args.note),
            ("client_ref", args.ref),
            ("fallback", args.fallback),
            ("merge", "true" if args.merge else None),
        )
        if value is not None
    ]
    fields += [("tags", tag) for tag in args.tag]
    files = collect([Path(path) for path in args.paths])
    with connected(_endpoint(args)) as client:
        result = client.publish(files, fields, artifact_id=args.update, archive=args.zip)
    if args.json:
        return _json(result)
    expiry = result.get("expires_at")
    _say(
        f"published {result['id']} v{result['version']}: a private link"
        + (f", expiring {expiry}" if expiry else " (the artifact does not expire)")
    )
    for warning in result.get("warnings", ()):
        _say(f"warning: {warning}")
    print(result["url"])
    return OK


def _list(args: argparse.Namespace) -> int:
    """List artifacts, newest first; ``--all`` follows the cursors to the end."""
    params = {
        name: str(value)
        for name, value in (
            ("query", args.query),
            ("tag", args.tag),
            ("project", args.project),
            ("visibility", args.visibility),
            ("limit", PAGE_SIZE if args.all else args.limit),
        )
        if value is not None
    }
    items: list[Json] = []
    with connected(_endpoint(args)) as client:
        while True:
            page = client.list(params)
            items += page["items"]
            if not (args.all and page.get("next_cursor")):
                break
            params["cursor"] = page["next_cursor"]
    if args.json:
        return _json(items)
    for item in items:
        print(f"{item['id']}\t{item['visibility']}\tv{item['version']}\t{item['title']}")
    return OK


def _get(args: argparse.Namespace) -> int:
    """Show one artifact: metadata, files, live links and views."""
    with connected(_endpoint(args)) as client:
        detail = client.get(args.id)
    if args.json:
        return _json(detail)
    artifact = detail["artifact"]
    print(f"{artifact['id']}  {artifact['title']}")
    print(f"  {artifact['visibility']}, version {detail['version']}, entry {artifact['entry']}")
    for file in detail["files"]:
        print(f"  file  {file['path']} ({file['bytes']} bytes)")
    for link in detail["links"]:
        lock = ", passcode" if link["passcode"] else ""
        print(f"  link  {link['lid']} {link['label'] or '-'} until {link['expires_at']}{lock}")
    views = detail.get("views")
    if views is not None:
        print(f"  views {views['total']} ({views['visitors']} visitors, {views['bots']} bots)")
    return OK


def _link(args: argparse.Namespace) -> int:
    """Mint a fresh private link; a generated passcode is printed once, on stderr."""
    body: Json = {}
    if args.ttl is not None:
        body["ttl"] = args.ttl
    if args.label is not None:
        body["label"] = args.label
    if args.passcode is not None:
        body["passcode"] = True if args.passcode == GENERATE else args.passcode
    with connected(_endpoint(args)) as client:
        link = client.link(args.id, body)
    if args.json:
        return _json(link)
    if link.get("passcode"):
        _say(f"passcode: {link['passcode']} (shown once: send it apart from the link)")
    print(link["url"])
    return OK


def _revoke(args: argparse.Namespace) -> int:
    """Revoke one link by its ``lid``, or every private link."""
    with connected(_endpoint(args)) as client:
        result = client.revoke(args.id, {} if args.link is None else {"link": args.link})
    if args.json:
        return _json(result)
    _say(f"revoked {'link ' + args.link if args.link else 'every private link'} of {args.id}")
    return OK


def _tag(args: argparse.Namespace) -> int:
    """Edit the title, tags or project without a new version."""
    body: Json = {}
    if args.add:
        body["add_tags"] = args.add
    if args.remove:
        body["remove_tags"] = args.remove
    if args.project is not None:
        body["project"] = args.project
    if args.title is not None:
        body["title"] = args.title
    if not body:
        raise ConfigurationError("name something to change: --add, --remove, --project or --title")
    with connected(_endpoint(args)) as client:
        result = client.meta(args.id, body)
    if args.json:
        return _json(result)
    tags = ", ".join(result["tags"]) or "-"
    _say(f"{result['id']}: tags {tags}, project {result['project'] or '-'}")
    return OK


def _delete(args: argparse.Namespace) -> int:
    """Delete an artifact; asks on a terminal, and needs ``--yes`` without one."""
    if not args.yes:
        if not sys.stdin.isatty():
            raise ConfigurationError("not a terminal: pass --yes to delete without asking")
        answer = input(f"delete {args.id} and every version of it? [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            _say("nothing deleted")
            return FAILED
    with connected(_endpoint(args)) as client:
        result = client.delete(args.id)
    if args.json:
        return _json(result)
    _say(f"deleted {result['deleted']}")
    return OK


def _open(args: argparse.Namespace) -> int:
    """Mint a fresh private link and open it in the system browser."""
    with connected(_endpoint(args)) as client:
        link = client.link(args.id, {})
    print(link["url"])
    if not webbrowser.open(link["url"]):
        _say("no browser to open: the link is above")
    return OK


def _endpoint(args: argparse.Namespace) -> Endpoint:
    return Endpoint.resolve(args.url, args.token_file)


def _json(value: Any) -> int:  # noqa: ANN401 - whatever the API answered
    print(json.dumps(value, indent=2, ensure_ascii=False))
    return OK


def _say(message: str) -> None:
    print(message, file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    common = _Parser(add_help=False)
    common.add_argument("--url", help="the server, e.g. https://vitrine.example (or VITRINE_URL)")
    common.add_argument(
        "--token-file", type=Path, help="a file holding the token (default: VITRINE_TOKEN)"
    )
    common.add_argument("--json", action="store_true", help="print the API's JSON response")

    parser = _Parser(
        prog=PROG,
        description="Publish files to a vitrine server and manage their private links.",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    commands = parser.add_subparsers(required=True, metavar="command", parser_class=_Parser)

    _publish_parser(commands, common)

    listing = commands.add_parser("list", parents=[common], help="list artifacts, newest first")
    listing.add_argument("--query", help="words that must all match")
    listing.add_argument("--tag")
    listing.add_argument("--project")
    listing.add_argument("--visibility", choices=["private", "public"])
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--all", action="store_true", help="every page, not just the first")
    listing.set_defaults(command=_list)

    get = commands.add_parser("get", parents=[common], help="metadata, files, links and views")
    get.add_argument("id")
    get.set_defaults(command=_get)

    link = commands.add_parser("link", parents=[common], help="mint a fresh private link")
    link.add_argument("id")
    link.add_argument("--ttl", metavar="DURATION", help="how long it works: 12h, 7d")
    link.add_argument("--label", help="who it is for; never shown to viewers")
    link.add_argument(
        "--passcode",
        nargs="?",
        const=GENERATE,
        metavar="VALUE",
        help="require a passcode; alone, one is generated and printed once on stderr",
    )
    link.set_defaults(command=_link)

    revoke = commands.add_parser("revoke", parents=[common], help="revoke one link, or all")
    revoke.add_argument("id")
    revoke.add_argument("--link", metavar="LID", help="only this link")
    revoke.set_defaults(command=_revoke)

    tag = commands.add_parser("tag", parents=[common], help="edit tags, project or title")
    tag.add_argument("id")
    tag.add_argument("--add", action="append", default=[], metavar="TAG")
    tag.add_argument("--remove", action="append", default=[], metavar="TAG")
    tag.add_argument("--project", help="an empty string removes it from its project")
    tag.add_argument("--title")
    tag.set_defaults(command=_tag)

    delete = commands.add_parser("delete", parents=[common], help="delete an artifact")
    delete.add_argument("id")
    delete.add_argument("--yes", action="store_true", help="do not ask")
    delete.set_defaults(command=_delete)

    opened = commands.add_parser("open", parents=[common], help="open a fresh link in a browser")
    opened.add_argument("id")
    opened.set_defaults(command=_open)
    return parser


def _publish_parser(commands: Any, common: _Parser) -> None:  # noqa: ANN401 - argparse's private type
    """``vitrine publish``, the command with the most options."""
    publish = commands.add_parser(
        "publish", parents=[common], help="publish files and folders as one PRIVATE artifact"
    )
    publish.add_argument("paths", nargs="+", metavar="PATH", help="files and folders")
    publish.add_argument("--title", help="default: the first path's name")
    publish.add_argument("--entry", help="the file the link opens")
    publish.add_argument("--tag", action="append", default=[], help="repeatable")
    publish.add_argument("--project", help="the project the artifact belongs to")
    publish.add_argument("--expires", metavar="DURATION", help="delete after: 30m, 12h, 7d")
    publish.add_argument("--note", help="what changed, kept in the history")
    publish.add_argument("--ref", help="your key for this publish: a retry returns the same one")
    publish.add_argument("--fallback", help="the file a missing page serves (index.html, 404.html)")
    publish.add_argument("--zip", action="store_true", help="send one archive (for a built site)")
    publish.add_argument("--update", metavar="ID", help="add a version to this artifact")
    publish.add_argument("--merge", action="store_true", help="with --update: keep other files")
    publish.set_defaults(command=_publish)
