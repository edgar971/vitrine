"""Configuration: flags, ``VITRINE_*`` variables and an optional TOML file (spec § 8)."""

from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from collections.abc import Mapping
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined
from pydantic_settings import BaseSettings, NoDecode, PydanticBaseSettingsSource, SettingsConfigDict

from mcp_vitrine.domain import layout
from mcp_vitrine.domain.errors import ConfigError, Invalid
from mcp_vitrine.domain.models import PublicMode, Scope

SECRET_KEYS = ("secret", "tokens", "admin_password")
CONFIG_TABLE = "vitrine"
ENV_PREFIX = "VITRINE_"
# What MCP clients and the Hermes plugin read (README): a shell that runs both sets them.
CLIENT_ENV_KEYS = frozenset({"VITRINE_URL", "VITRINE_TOKEN"})
MIN_SECRET_BYTES = 32
RENDERERS = frozenset({"md", "csv", "tsv", "json", "jsx"})
# The longest lifetime a caller may ask for; past it a date stops being representable.
MAX_REQUESTED_DURATION = timedelta(days=36500)

_SIZE = re.compile(r"^(\d+)([KMGT]?B?)$", re.IGNORECASE)
# Twelve digits is longer than any lifetime; past ~4300, int() itself refuses the string.
_DURATION = re.compile(r"^(\d{1,12})([smhd]?)$", re.IGNORECASE)
_RATE = re.compile(r"^(\d+)/([smh])$", re.IGNORECASE)
_TOKEN_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# What `mcp-vitrine admin password` prints: scrypt$n$r$p$salt$hash, base64url without padding.
_ADMIN_HASH = re.compile(
    r"^scrypt\$\d{1,10}\$\d{1,4}\$\d{1,4}\$[A-Za-z0-9_-]{16,}\$[A-Za-z0-9_-]{32,}$"
)

_SCALE = {
    "": 1,
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "M": 1024**2,
    "MB": 1024**2,
    "G": 1024**3,
    "GB": 1024**3,
    "T": 1024**4,
    "TB": 1024**4,
}
_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
_TICKS = {"s": 1, "m": 60, "h": 3600}


class TickRate(BaseModel):
    """A token-bucket rate: ``count`` calls per ``seconds`` (spec § 7.5)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    count: int = Field(gt=0)
    seconds: int = Field(gt=0)

    def per_second(self) -> float:
        """How many tokens come back each second."""
        return self.count / self.seconds


class TokenEntry(BaseModel):
    """One client token as the server keeps it: a name, a SHA-256 and scopes (spec § 8.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    sha256: str
    scopes: frozenset[Scope]


class ViewLogMode(StrEnum):
    """``view_log`` (spec § 11.4): count views per artifact and link, or record nothing."""

    COUNTS = "counts"
    OFF = "off"


class LogLevel(StrEnum):
    """``log_level`` (spec § 8.2)."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LogFormat(StrEnum):
    """``log_format`` (spec § 8.2)."""

    TEXT = "text"
    JSON = "json"


def parse_size(value: object) -> int:
    """Parse ``512KB``/``100MB``/``10GB`` in binary units, or a plain byte count (spec § 8.1)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if not isinstance(value, str):
        raise ConfigError(f"a size must be a string or a number, not {type(value).__name__}")
    match = _SIZE.fullmatch(value.strip())
    if match is None:
        raise ConfigError(f"{value!r} is not a size: use 512KB, 100MB, 10GB or a byte count")
    number, unit = match.groups()
    return int(number) * _SCALE[unit.upper()]


def parse_duration(value: object) -> timedelta:
    """Parse ``30s``/``15m``/``1h``/``7d``, or a plain number of seconds (spec § 8.1)."""
    if isinstance(value, timedelta):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return _positive_seconds(value)
    if not isinstance(value, str):
        raise ConfigError(f"a duration must be a string or a number, not {type(value).__name__}")
    match = _DURATION.fullmatch(value.strip())
    if match is None:
        raise ConfigError(f"{value!r} is not a duration: use 30s, 15m, 1h or 7d")
    number, unit = match.groups()
    return _positive_seconds(int(number) * _SECONDS[unit.lower()])


def parse_optional_duration(value: object) -> timedelta | None:
    """A duration, or ``off`` for none at all (spec § 8.1)."""
    if value is None or value == "off":
        return None
    return parse_duration(value)


def requested_duration(value: str) -> timedelta:
    """A duration a caller sent: a bad one is the caller's mistake (400), not the operator's."""
    try:
        duration = parse_duration(value)
    except ConfigError as failure:
        raise Invalid(str(failure)) from None
    if duration > MAX_REQUESTED_DURATION:
        raise Invalid(f"a duration cannot exceed {MAX_REQUESTED_DURATION.days}d")
    return duration


def parse_rate(value: object) -> TickRate | None:
    """Parse ``60/m``, ``1200/h`` or ``off``, which disables the bucket (spec § 7.5)."""
    if value is None or value == "off":
        return None
    if isinstance(value, TickRate):
        return value
    if not isinstance(value, str):
        raise ConfigError(f"a rate must be a string or a number, not {type(value).__name__}")
    match = _RATE.fullmatch(value.strip().lower())
    if match is None:
        raise ConfigError(f"{value!r} is not a rate: use 60/m, 1200/h or off")
    count, unit = match.groups()
    return TickRate(count=int(count), seconds=_TICKS[unit])


def parse_list(value: object) -> list[str]:
    """Parse a comma-separated environment value; TOML already gives an array (spec § 8.1)."""
    if isinstance(value, list):
        return [str(item) for item in value]
    if not isinstance(value, str):
        raise ConfigError(f"a list must be comma-separated, not {type(value).__name__}")
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_tokens(value: object) -> dict[str, TokenEntry]:
    """Parse ``name:sha256hex[:read+write]`` entries, comma-separated (spec § 8.2)."""
    if not isinstance(value, str):
        raise ConfigError("tokens come from VITRINE_TOKENS, as name:sha256hex[:read+write] entries")
    entries: dict[str, TokenEntry] = {}
    for chunk in parse_list(value):
        name, _, rest = chunk.partition(":")
        digest, _, scopes = rest.partition(":")
        if not name or not digest or ":" in scopes:
            raise ConfigError("a token entry must be name:sha256hex[:read+write]")
        if _TOKEN_NAME.fullmatch(name) is None:
            raise ConfigError(f"invalid token name {name!r}: [a-z0-9][a-z0-9_.-]{{0,63}}")
        if _SHA256.fullmatch(digest) is None:
            raise ConfigError("a token hash must be 64 lowercase hex characters")
        if name in entries:
            raise ConfigError(f"duplicate token name {name!r}")
        entries[name] = TokenEntry(name=name, sha256=digest, scopes=Scope.of(scopes))
    if not entries:
        raise ConfigError("at least one token is required in VITRINE_TOKENS")
    return entries


def plain_http_warning(settings: Settings) -> str | None:
    """The warning for tokens and links crossing a network in clear text (security R1)."""
    if urlsplit(settings.public_base_url).scheme != "http" or _is_loopback(settings.host):
        return None
    return (
        f"public_base_url is http:// while vitrine listens on {settings.host}: tokens and private "
        "links cross the network in clear text; serve it over https"
    )


def _is_loopback(host: str) -> bool:
    """Whether a bind address only accepts connections from this machine."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def unscoped_tokens(value: str) -> list[str]:
    """The names of ``VITRINE_TOKENS`` entries that omit scopes, and so get all four (§ 8.2)."""
    entries = [chunk.split(":") for chunk in parse_list(value)]
    return [parts[0] for parts in entries if len(parts) <= len(("name", "sha256"))]


ByteSize = Annotated[int, BeforeValidator(parse_size)]
Duration = Annotated[timedelta, BeforeValidator(parse_duration)]
OptionalDuration = Annotated[timedelta | None, BeforeValidator(parse_optional_duration)]
Rate = Annotated[TickRate | None, BeforeValidator(parse_rate)]
CommaSeparated = Annotated[list[str], NoDecode, BeforeValidator(parse_list)]
Tokens = Annotated[dict[str, TokenEntry], NoDecode, BeforeValidator(parse_tokens)]


def _positive_seconds(value: int) -> timedelta:
    """Wrap a second count, refusing the zero and negative forms a duration cannot have."""
    if value <= 0:
        raise ConfigError("a duration must be positive")
    try:
        return timedelta(seconds=value)
    except OverflowError:
        raise ConfigError("a duration is too long") from None


class MappingSource(PydanticBaseSettingsSource):
    """A settings layer over a mapping that was already read and validated."""

    def __init__(self, settings_cls: type[BaseSettings], values: Mapping[str, object]) -> None:
        """Keep the layer's values."""
        super().__init__(settings_cls)
        self._values = dict(values)

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[object, str, bool]:
        """One field's value, if this layer has it."""
        return self._values.get(field_name), field_name, field_name in self._values

    def __call__(self) -> dict[str, object]:
        """The whole layer; the settings class validates it."""
        return dict(self._values)


SERVING, SECRETS, POLICY, LIMITS, LINKS, OPERATIONS = (
    "Serving",
    "Secrets (environment only)",
    "Policy",
    "Limits",
    "Links",
    "Operations",
)
GROUP = "group"


def setting(
    default: object = PydanticUndefined,
    *,
    group: str,
    description: str,
    ge: int | None = None,
    le: int | None = None,
) -> Any:  # noqa: ANN401 - a pydantic Field, typed as whatever the annotation says
    """A settings field carrying the group and description ``docs/configuration.md`` shows.

    Defaults are written the way an operator would (``"100MB"``, ``"7d"``) and parsed like any
    other value, so the reference prints exactly what the code uses.
    """
    return Field(default, description=description, json_schema_extra={GROUP: group}, ge=ge, le=le)


class Settings(BaseSettings):
    """The effective configuration, keyed exactly as spec § 8.2 documents it."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX, extra="forbid", frozen=True, validate_default=True
    )

    data_dir: Path = setting("./data", group=SERVING, description="Storage root (spec § 4.1)")
    public_base_url: str = setting(
        "http://localhost:8080",
        group=SERVING,
        description="Origin used to build every URL; an origin, with no path",
    )
    host: str = setting("127.0.0.1", group=SERVING, description="Listen address")
    port: int = setting(8080, group=SERVING, description="Listen port", ge=1, le=65535)
    allowed_hosts: CommaSeparated = setting(
        [], group=SERVING, description="Extra `Host` values accepted besides the base URL's"
    )
    forwarded_allow_ips: CommaSeparated = setting(
        ["127.0.0.1"],
        group=SERVING,
        description="Proxies whose `X-Forwarded-*` headers are trusted",
    )

    secret: SecretStr = setting(
        group=SECRETS,
        description=(
            "HMAC key for links, at least 32 bytes. Rotating it revokes every private link."
        ),
    )
    tokens: Tokens = setting(
        group=SECRETS,
        description=(
            "Client tokens as `name:sha256hex[:scope+scope…]`, comma-separated; at least one. "
            "Omitted scopes mean all four."
        ),
    )
    admin_password: SecretStr | None = setting(
        None,
        group=SECRETS,
        description=(
            "The dashboard's operator password as the scrypt hash `mcp-vitrine admin password` "
            "prints; unset, the dashboard is off"
        ),
    )

    public_mode: PublicMode = setting(
        PublicMode.CONFIRM,
        group=POLICY,
        description="`confirm`, `disabled` or `unrestricted` (spec § 6)",
    )
    renderers: CommaSeparated = setting(
        ["md", "csv", "tsv", "json", "jsx"],
        group=POLICY,
        description="Renderers to enable; `[]` serves everything raw",
    )
    jsx_cdn: str = setting(
        "",
        group=POLICY,
        description="Where the `jsx` renderer loads packages it does not ship, e.g. "
        "`https://esm.sh/`; empty: nothing is loaded from outside vitrine",
    )

    max_upload: ByteSize = setting(
        "100MB", group=LIMITS, description="Request body limit on `/api` and `/u`"
    )
    max_mcp_body: ByteSize = setting(
        "4MB", group=LIMITS, description="Request body limit on `/mcp`"
    )
    max_artifact: ByteSize = setting("100MB", group=LIMITS, description="Largest single version")
    max_files: int = setting(200, group=LIMITS, description="Files per version", ge=1)
    unpack_max_ratio: int = setting(
        200,
        group=LIMITS,
        description="Most an archive entry may inflate past its compressed size, once over 1 MiB",
        ge=1,
    )
    quota: ByteSize = setting(
        "10GB", group=LIMITS, description="Total storage; writes beyond it fail with 507"
    )
    retain_versions: int = setting(10, group=LIMITS, description="Versions kept per artifact", ge=1)
    ref_ttl: Duration = setting(
        "24h", group=LIMITS, description="How long a `client_ref` stays bound (spec § 4.5)"
    )
    rate_writes: Rate = setting("60/m", group=LIMITS, description="Per-client write bucket")
    rate_links: Rate = setting("120/m", group=LIMITS, description="Per-client link bucket")
    rate_reads: Rate = setting("600/m", group=LIMITS, description="Per-client read bucket")
    rate_views: Rate = setting("1200/m", group=LIMITS, description="Per-IP view bucket")
    rate_unlock: Rate = setting(
        "10/m",
        group=LIMITS,
        description="Wrong passcodes per link and source IP; a right one spends nothing",
    )
    rate_unlock_link: Rate = setting(
        "100/h", group=LIMITS, description="Wrong passcodes per link, from every source together"
    )
    rate_admin_login: Rate = setting(
        "5/m", group=LIMITS, description="Wrong dashboard passwords per source IP"
    )
    max_links: int = setting(
        500, group=LIMITS, description="Live recorded links per artifact", ge=1
    )

    link_ttl: Duration = setting("7d", group=LINKS, description="Default private link lifetime")
    link_ttl_max: Duration = setting(
        "90d", group=LINKS, description="Longest private link lifetime"
    )
    upload_ttl: Duration = setting("15m", group=LINKS, description="Default upload URL lifetime")
    upload_ttl_max: Duration = setting("1h", group=LINKS, description="Longest upload URL lifetime")
    unlock_ttl: Duration = setting(
        "12h",
        group=LINKS,
        description="How long an unlocked passcode link works, at most until the link expires",
    )
    public_ttl_max: OptionalDuration = setting(
        "off",
        group=LINKS,
        description="Longest public window: every public artifact goes private after it; "
        "`off` for none",
    )

    sweep_interval: Duration = setting(
        "1h",
        group=OPERATIONS,
        description="Period of the expiry, staging, idempotency-record and audit-retention sweep",
    )
    audit_retention: Duration = setting(
        "365d", group=OPERATIONS, description="Audit months older than this are deleted"
    )
    view_log: ViewLogMode = setting(
        ViewLogMode.COUNTS,
        group=OPERATIONS,
        description="`counts` per artifact, link and hour (no IPs, no user agents), or `off`",
    )
    view_retention: Duration = setting(
        "90d", group=OPERATIONS, description="View-count months older than this are deleted"
    )
    metrics_port: int = setting(
        0,
        group=OPERATIONS,
        description="Prometheus listener, `0` for off; needs the `metrics` extra",
        ge=0,
        le=65535,
    )
    log_level: LogLevel = setting(LogLevel.INFO, group=OPERATIONS, description="Log level")
    log_format: LogFormat = setting(
        LogFormat.TEXT, group=OPERATIONS, description="`text` or `json`"
    )

    @field_validator("secret")
    @classmethod
    def _secret_is_long_enough(cls, value: SecretStr) -> SecretStr:
        """Require 32 bytes, because this key signs every private link (spec § 8.2)."""
        if len(value.get_secret_value().encode()) < MIN_SECRET_BYTES:
            raise ValueError(f"the secret must be at least {MIN_SECRET_BYTES} bytes")
        return value

    @field_validator("admin_password")
    @classmethod
    def _admin_password_is_a_hash(cls, value: SecretStr | None) -> SecretStr | None:
        """Only the hash is accepted, so a password pasted as itself fails at startup (§ 8.2)."""
        if value is not None and _ADMIN_HASH.fullmatch(value.get_secret_value()) is None:
            raise ValueError(
                "admin_password is the scrypt$… hash `mcp-vitrine admin password` prints, "
                "not the password"
            )
        return value

    @field_validator("public_base_url")
    @classmethod
    def _public_base_url_is_an_origin(cls, value: str) -> str:
        """Reduce the base URL to its origin; every link is built from it (spec § 8.2)."""
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc or "@" in parts.netloc:
            raise ValueError("public_base_url must be an absolute http(s) origin")
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("public_base_url cannot carry a path, a query or a fragment")
        return f"{parts.scheme}://{parts.netloc}"

    @field_validator("renderers")
    @classmethod
    def _renderers_are_built_in(cls, value: list[str]) -> list[str]:
        """Only the renderers vitrine ships exist (spec § 8.2)."""
        unknown = sorted(set(value) - RENDERERS)
        if unknown:
            raise ValueError(f"unknown renderer(s): {', '.join(unknown)}")
        return value

    @field_validator("jsx_cdn")
    @classmethod
    def _jsx_cdn_is_a_base_url(cls, value: str) -> str:
        """Empty, or an ``https`` URL ending in ``/`` that a package name is appended to."""
        if value and (urlsplit(value).scheme != "https" or not value.endswith("/")):
            raise ValueError("jsx_cdn is an https URL ending in /, or empty")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _log_level_is_upper_case(cls, value: object) -> object:
        """Accept ``debug`` as well as ``DEBUG`` in the environment (spec § 8.2)."""
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _keys_agree_with_each_other(self) -> Settings:
        """The startup rules that compare two keys (spec § 8.2)."""
        if self.link_ttl > self.link_ttl_max:
            raise ValueError("link_ttl cannot exceed link_ttl_max")
        if self.upload_ttl > self.upload_ttl_max:
            raise ValueError("upload_ttl cannot exceed upload_ttl_max")
        if self.max_artifact > self.quota:
            raise ValueError("max_artifact cannot exceed quota")
        base = urlsplit(self.public_base_url)
        if (
            self.admin_password is not None
            and base.scheme == "http"
            and not _is_loopback(base.hostname or "")
        ):
            raise ValueError(
                "the dashboard needs an https public_base_url, or a loopback one such as "
                "http://127.0.0.1:8080"
            )
        return self


def read_config_file(path: Path) -> dict[str, object]:
    """Read a TOML config file, rejecting secrets and unknown keys (spec § 8.1)."""
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigError(f"cannot read {path}: {error.strerror or error}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path} is not valid TOML: {error}") from error
    if CONFIG_TABLE in parsed:
        strays = sorted(set(parsed) - {CONFIG_TABLE})
        if strays:
            raise ConfigError(
                f"{path}: keys must live in [{CONFIG_TABLE}] or at the top level, not both"
            )
        table = parsed[CONFIG_TABLE]
    else:
        table = parsed
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: [{CONFIG_TABLE}] must be a table")
    values: dict[str, object] = dict(table)
    for key in SECRET_KEYS:
        if key in values:
            raise ConfigError(
                f"{path}: {key} is a secret and comes from the environment (VITRINE_{key.upper()})"
            )
    unknown = sorted(set(values) - set(Settings.model_fields))
    if unknown:
        raise ConfigError(f"{path}: unknown key(s): {', '.join(unknown)}")
    return values


def _unknown_env_keys() -> list[str]:
    """Environment variables carrying the prefix that name no settings field (spec § 8.1).

    pydantic-settings reads only the variables that match a field, so a typo like
    ``VITRINE_MAX_UPLAOD`` would otherwise be ignored as quietly as it was mistyped.
    """
    fields = {name.lower() for name in Settings.model_fields}
    return sorted(
        key
        for key in os.environ
        if key.upper().startswith(ENV_PREFIX)
        and key[len(ENV_PREFIX) :].lower() not in fields
        and key.upper() not in CLIENT_ENV_KEYS
    )


def load_settings(
    *, config_path: Path | None = None, flags: Mapping[str, object] | None = None
) -> Settings:
    """Build the effective settings: flags, then ``VITRINE_*``, then the file (spec § 8.1)."""
    unknown = _unknown_env_keys()
    if unknown:
        raise ConfigError(f"unknown environment key(s): {', '.join(unknown)}")
    file_values = read_config_file(config_path) if config_path is not None else {}

    class Configured(Settings):
        """The settings class carrying this call's flags and config file."""

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            """Order the layers, highest precedence first (spec § 8.1)."""
            return (
                init_settings,
                MappingSource(settings_cls, flags or {}),
                env_settings,
                MappingSource(settings_cls, file_values),
            )

    try:
        settings = Configured()
    except ValidationError as error:
        raise ConfigError(_describe(error)) from error
    layout.check_root(settings.data_dir)
    return settings


def _describe(error: ValidationError) -> str:
    """One line naming what is wrong, so a startup failure is actionable."""
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'settings'}: {item['msg']}"
        for item in error.errors()
    )
