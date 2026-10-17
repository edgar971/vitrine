"""``LinkService``: ids, and the two signed link kinds (spec § 3)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict

from mcp_vitrine.config import Settings
from mcp_vitrine.domain.clock import Clock
from mcp_vitrine.domain.errors import NotFound
from mcp_vitrine.domain.ids import is_valid_id, is_valid_lid, new_nonce
from mcp_vitrine.domain.models import Artifact, LinkClaims, LinkKind, SignedLink, UploadLink

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
    unlock_ttl: timedelta = timedelta(hours=12)
    max_links: int = 500
    public_ttl_max: timedelta | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> LinkPolicy:
        """Take the link keys from the effective configuration."""
        return cls(
            base_url=settings.public_base_url,
            link_ttl=settings.link_ttl,
            link_ttl_max=settings.link_ttl_max,
            upload_ttl=settings.upload_ttl,
            upload_ttl_max=settings.upload_ttl_max,
            unlock_ttl=settings.unlock_ttl,
            max_links=settings.max_links,
            public_ttl_max=settings.public_ttl_max,
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

    @property
    def policy(self) -> LinkPolicy:
        """The link settings this signer was built with."""
        return self._policy

    def view_url(
        self,
        artifact_id: str,
        *,
        epoch: int,
        ttl: timedelta | None = None,
        expires_at: datetime | None = None,
        lid: str | None = None,
    ) -> SignedLink:
        """Mint a private viewing link that stops working at its expiry (spec § 3).

        With ``lid`` it is a recorded link, which works only while its record exists.
        ``expires_at`` is one :meth:`link_expiry` already chose, for a link and its record.
        """
        if expires_at is None:
            expires_at = self.link_expiry(ttl)
        payload = (str(epoch),) if lid is None else (str(epoch), lid)
        signature = self._sign(self._body(LinkKind.VIEW, artifact_id, expires_at, *payload))
        return SignedLink(url=f"{self._policy.base_url}/s/{signature}/", expires_at=expires_at)

    def link_expiry(self, ttl: timedelta | None) -> datetime:
        """When a view link asked to last ``ttl`` expires, clamped to ``link_ttl_max``."""
        return self._expiry(ttl, self._policy.link_ttl, self._policy.link_ttl_max)

    def unlocked(self, claims: LinkClaims, *, link_expires_at: datetime) -> str:
        """The signature a right passcode earns: ``unlock_ttl`` long, never past the link (§ 3)."""
        if claims.lid is None or claims.epoch is None:
            raise NotFound()
        expires_at = min(self._clock.now() + self._policy.unlock_ttl, link_expires_at)
        return self._sign(
            self._body(LinkKind.UNLOCKED, claims.id, expires_at, str(claims.epoch), claims.lid)
        )

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

    def verify_unlocked(self, signature: str) -> LinkClaims:
        """Return the claims of a valid unlocked signature, or ``NotFound``."""
        return self._verify(signature, LinkKind.UNLOCKED)

    def verify_upload(self, signature: str) -> LinkClaims:
        """Return the claims of a valid upload link, or ``NotFound``."""
        return self._verify(signature, LinkKind.UPLOAD)

    def gate_state(
        self, artifact_id: str, *, version: int, client: str, digest: str, ttl: timedelta
    ) -> str:
        """Sign what a gate question was about, for the answer to prove (security T20).

        The answer comes back on a later call, so the state names the artifact, the version
        the human was shown, the client that asked and a digest of the request.
        """
        expires_at = self._clock.now() + ttl
        return self._sign(
            self._body(LinkKind.GATE, artifact_id, expires_at, str(version), client, digest)
        )

    def verify_gate_state(self, state: str, *, artifact_id: str, client: str, digest: str) -> int:
        """The version a valid state was minted for; ``NotFound`` for any other state."""
        body = self._proven(state)
        try:
            kind, claimed_id, expires, version, claimed_client, claimed_digest = (
                body.decode().split(SEPARATOR)
            )
            expires_at = datetime.fromtimestamp(int(expires), tz=UTC)
        except UnicodeDecodeError, ValueError:
            raise NotFound() from None
        matches = (kind, claimed_id, claimed_client, claimed_digest) == (
            LinkKind.GATE,
            artifact_id,
            client,
            digest,
        )
        if not matches or expires_at <= self._clock.now() or not version.isdigit():
            raise NotFound()
        return int(version)

    def accepts(self, claims: LinkClaims, artifact: Artifact) -> bool:
        """Whether a verified link still names this artifact at its current epoch (spec § 3).

        A recorded link (one with a ``lid``) also needs its record: deleting it revokes the link.
        """
        if claims.id != artifact.id or claims.epoch != artifact.link_epoch:
            return False
        return claims.lid is None or artifact.link(claims.lid) is not None

    def _sign(self, body: str) -> str:
        """``b64url(body) "." b64url(HMAC-SHA256(secret, body)[0:16])``."""
        mac = hmac.new(self._secret, body.encode(), hashlib.sha256).digest()[:MAC_BYTES]
        return f"{_encode(body.encode())}.{_encode(mac)}"

    def _verify(self, signature: str, kind: LinkKind) -> LinkClaims:
        """Check the MAC, the kind and the expiry, in that order, before believing anything."""
        return self._claims(self._proven(signature), kind)

    def _proven(self, signature: str) -> bytes:
        """The body a signature carries, once its MAC checks out; ``NotFound`` otherwise."""
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
        return body

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
            case LinkKind.VIEW, [epoch] if _is_count(epoch):
                return LinkClaims(
                    kind=kind, id=artifact_id, expires_at=expires_at, epoch=int(epoch)
                )
            case ((LinkKind.VIEW | LinkKind.UNLOCKED), [epoch, lid]) if _is_count(
                epoch
            ) and is_valid_lid(lid):
                return LinkClaims(
                    kind=kind, id=artifact_id, expires_at=expires_at, epoch=int(epoch), lid=lid
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


def _is_count(text: str) -> bool:
    """Whether a signed field is a plain non-negative integer."""
    return text.isascii() and text.isdigit()


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
