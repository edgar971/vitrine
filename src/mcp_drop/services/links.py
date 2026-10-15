"""``LinkService``: ids, and the two signed link kinds (spec § 3)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict

from mcp_drop.config import Settings
from mcp_drop.domain.clock import Clock
from mcp_drop.domain.errors import NotFound
from mcp_drop.domain.ids import is_valid_id, new_nonce
from mcp_drop.domain.models import Artifact, LinkClaims, LinkKind, SignedLink, UploadLink

MAC_BYTES = 16
SIGNATURE_PARTS = 2
BASE64URL = re.compile(r"^[A-Za-z0-9_-]*$")
SEPARATOR = "|"


class LinkPolicy(BaseModel):
    """The link settings a signer needs (spec § 8.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str
    link_ttl: timedelta = timedelta(days=7)
    link_ttl_max: timedelta = timedelta(days=90)
    upload_ttl: timedelta = timedelta(minutes=15)
    upload_ttl_max: timedelta = timedelta(hours=1)

    @classmethod
    def from_settings(cls, settings: Settings) -> LinkPolicy:
        """Take the link keys from the effective configuration."""
        return cls(
            base_url=settings.public_base_url,
            link_ttl=settings.link_ttl,
            link_ttl_max=settings.link_ttl_max,
            upload_ttl=settings.upload_ttl,
            upload_ttl_max=settings.upload_ttl_max,
        )


class LinkService:
    """Signs and verifies view and upload links; the kind is inside the MAC.

    Every failure answers with the same ``NotFound`` as an unknown artifact, so a caller cannot
    tell a tampered link from a missing one (spec § 3, § 10).
    """

    def __init__(self, policy: LinkPolicy, secret: bytes, clock: Clock) -> None:
        """Hold the origin the URLs are built from, the HMAC key and the clock."""
        self._policy = policy
        self._secret = secret
        self._clock = clock

    def view_url(self, artifact_id: str, *, epoch: int, ttl: timedelta | None = None) -> SignedLink:
        """Mint a private viewing link that stops working at its expiry (spec § 3)."""
        expires_at = self._expiry(ttl, self._policy.link_ttl, self._policy.link_ttl_max)
        signature = self._sign(self._body(LinkKind.VIEW, artifact_id, expires_at, str(epoch)))
        return SignedLink(url=f"{self._policy.base_url}/s/{signature}/", expires_at=expires_at)

    def public_url(self, artifact_id: str) -> str:
        """The unsigned URL of a public artifact: the id is the secret (spec § 2)."""
        return f"{self._policy.base_url}/a/{artifact_id}/"

    def upload_url(
        self, artifact_id: str, *, client: str, ttl: timedelta | None = None
    ) -> UploadLink:
        """Mint a single-use upload URL for ``client``, with the nonce the store records (§ 7.3).

        The client is signed in, so the upload is attributed to whoever asked for the URL and
        spends that client's bucket, whoever ends up running the ``PUT``.
        """
        expires_at = self._expiry(ttl, self._policy.upload_ttl, self._policy.upload_ttl_max)
        nonce = new_nonce()
        signature = self._sign(self._body(LinkKind.UPLOAD, artifact_id, expires_at, nonce, client))
        return UploadLink(
            url=f"{self._policy.base_url}/u/{signature}", expires_at=expires_at, nonce=nonce
        )

    def verify_view(self, signature: str) -> LinkClaims:
        """Return the claims of a valid view link, or ``NotFound``."""
        return self._verify(signature, LinkKind.VIEW)

    def verify_upload(self, signature: str) -> LinkClaims:
        """Return the claims of a valid upload link, or ``NotFound``."""
        return self._verify(signature, LinkKind.UPLOAD)

    def accepts(self, claims: LinkClaims, artifact: Artifact) -> bool:
        """Whether a verified link still names this artifact at its current epoch (spec § 3)."""
        return claims.id == artifact.id and claims.epoch == artifact.link_epoch

    def _sign(self, body: str) -> str:
        """``b64url(body) "." b64url(HMAC-SHA256(secret, body)[0:16])``."""
        mac = hmac.new(self._secret, body.encode(), hashlib.sha256).digest()[:MAC_BYTES]
        return f"{_encode(body.encode())}.{_encode(mac)}"

    def _verify(self, signature: str, kind: LinkKind) -> LinkClaims:
        """Check the MAC, the kind and the expiry, in that order, before believing anything."""
        parts = signature.split(".")
        if len(parts) != SIGNATURE_PARTS:
            raise NotFound()
        body = _decode(parts[0])
        supplied = _decode(parts[1])
        if body is None or supplied is None:
            raise NotFound()
        expected = hmac.new(self._secret, body, hashlib.sha256).digest()[:MAC_BYTES]
        if not hmac.compare_digest(supplied, expected):
            raise NotFound()
        return self._claims(body, kind)

    def _claims(self, body: bytes, kind: LinkKind) -> LinkClaims:
        """Turn a proven body into claims, refusing anything the spec does not allow."""
        try:
            claimed_kind, artifact_id, expires, *payload = body.decode().split(SEPARATOR)
            expires_at = datetime.fromtimestamp(int(expires), tz=UTC)
        except UnicodeDecodeError, ValueError:
            raise NotFound() from None
        if claimed_kind != kind or not is_valid_id(artifact_id) or expires_at <= self._clock.now():
            raise NotFound()
        match kind, payload:
            case LinkKind.VIEW, [epoch] if epoch.isascii() and epoch.isdigit():
                return LinkClaims(
                    kind=kind, id=artifact_id, expires_at=expires_at, epoch=int(epoch)
                )
            case LinkKind.UPLOAD, [nonce, client] if nonce and client:
                return LinkClaims(
                    kind=kind, id=artifact_id, expires_at=expires_at, nonce=nonce, client=client
                )
            case _:
                raise NotFound()

    def _body(self, kind: LinkKind, artifact_id: str, expires_at: datetime, *payload: str) -> str:
        """The signed string: the kind first, so a signature cannot change into another kind."""
        return SEPARATOR.join((kind.value, artifact_id, str(int(expires_at.timestamp())), *payload))

    def _expiry(self, ttl: timedelta | None, default: timedelta, ceiling: timedelta) -> datetime:
        """Clamp the requested lifetime to the configured ceiling (spec § 3)."""
        now = self._clock.now()
        return now + min(default if ttl is None else ttl, ceiling)


def _encode(value: bytes) -> str:
    """URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode(text: str) -> bytes | None:
    """Undo :func:`_encode`, or ``None`` when the text is not base64url at all."""
    if BASE64URL.fullmatch(text) is None:
        return None
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except binascii.Error, ValueError:
        return None
