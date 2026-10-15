"""``LinkService``: the signed-link rules of spec § 3."""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Callable
from datetime import timedelta

import pytest

from mcp_drop.config import Settings
from mcp_drop.domain.clock import Clock
from mcp_drop.domain.errors import NotFound
from mcp_drop.domain.models import Artifact, LinkKind, Version
from mcp_drop.domain.paths import choose_entry
from mcp_drop.services.links import LinkPolicy, LinkService
from tests.conftest import FrozenClock

BASE = "https://drop.example.com"
SECRET = b"0123456789abcdef0123456789abcdef"
ID = "k7m2q9x4tz3wbn6r"
MAC_BYTES = 16


@pytest.fixture
def links(clock: Clock) -> LinkService:
    """A signer with a known secret and an origin."""
    return LinkService(LinkPolicy(base_url=BASE), SECRET, clock)


def _forge(body: str, secret: bytes = SECRET) -> str:
    """Sign an arbitrary body the way anyone holding the key would, for the tampering specs."""
    mac = hmac.new(secret, body.encode(), hashlib.sha256).digest()[:MAC_BYTES]
    return f"{_encode(body.encode())}.{_encode(mac)}"


def _signature(url: str, marker: str) -> str:
    """The signature segment of a link URL, as a caller would take it from the path."""
    return url.split(marker)[1].rstrip("/")


def _encode(value: bytes) -> str:
    """URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


@pytest.fixture
def artifact(clock: Clock) -> Artifact:
    """An artifact at epoch 0, for the epoch-acceptance specs."""
    now = clock.now()
    return Artifact(
        id=ID,
        title="Energy",
        entry=choose_entry(files=("index.html",), requested=None),
        version=1,
        versions=(Version(n=1, at=now, bytes=1, files=1),),
        created_at=now,
        updated_at=now,
        source="hermes",
    )


class DescribeViewLinks:
    class WhenOneIsMinted:
        def it_points_at_the_signed_view_path(self, links: LinkService) -> None:
            assert links.view_url(ID, epoch=0).url.startswith(f"{BASE}/s/")

        def it_is_one_path_segment_plus_a_slash(self, links: LinkService) -> None:
            assert links.view_url(ID, epoch=0).url.endswith("/")
            assert links.view_url(ID, epoch=0).url.count("/") == 5

        def it_expires_at_the_default_lifetime(self, links: LinkService, clock: Clock) -> None:
            assert links.view_url(ID, epoch=0).expires_at == clock.now() + timedelta(days=7)

        def it_uses_the_ttl_it_is_given(self, links: LinkService, clock: Clock) -> None:
            link = links.view_url(ID, epoch=0, ttl=timedelta(hours=1))
            assert link.expires_at == clock.now() + timedelta(hours=1)

        def it_clamps_the_ttl_to_the_ceiling(self, links: LinkService, clock: Clock) -> None:
            link = links.view_url(ID, epoch=0, ttl=timedelta(days=365))
            assert link.expires_at == clock.now() + timedelta(days=90)

    class WhenTheSignatureIsInspected:
        def it_is_url_safe_and_unpadded(self, links: LinkService) -> None:
            signature = _signature(links.view_url(ID, epoch=0).url, "/s/")
            assert not set(signature) & set("=+/")


class DescribeUploadLinks:
    class WhenOneIsMinted:
        def it_points_at_the_signed_upload_path(self, links: LinkService) -> None:
            assert links.upload_url(ID).url.startswith(f"{BASE}/u/")

        def it_carries_a_nonce(self, links: LinkService) -> None:
            assert links.upload_url(ID).nonce

        def it_is_single_use_when_it_is_read_back(self, links: LinkService) -> None:
            link = links.upload_url(ID)
            assert links.verify_upload(_signature(link.url, "/u/")).nonce == link.nonce

        def it_clamps_the_ttl_to_the_upload_ceiling(self, links: LinkService, clock: Clock) -> None:
            link = links.upload_url(ID, ttl=timedelta(days=365))
            assert link.expires_at == clock.now() + timedelta(hours=1)


class DescribeVerification:
    class WhenTheLinkIsIntact:
        def it_returns_the_id_and_the_epoch(self, links: LinkService) -> None:
            signature = _signature(links.view_url(ID, epoch=3).url, "/s/")
            claims = links.verify_view(signature)
            assert (claims.id, claims.epoch, claims.kind) == (ID, 3, LinkKind.VIEW)

    class WhenTheLinkIsOfTheOtherKind:
        def it_refuses_a_view_signature_used_to_upload(self, links: LinkService) -> None:
            signature = _signature(links.view_url(ID, epoch=0).url, "/s/")
            with pytest.raises(NotFound):
                links.verify_upload(signature)

        def it_refuses_an_upload_signature_used_to_view(self, links: LinkService) -> None:
            signature = _signature(links.upload_url(ID).url, "/u/")
            with pytest.raises(NotFound):
                links.verify_view(signature)

    class WhenTheSignatureIsTamperedWith:
        def it_refuses_a_changed_body(self, links: LinkService) -> None:
            signature = _signature(links.view_url(ID, epoch=0).url, "/s/")
            _, mac = signature.split(".")
            other_body = _encode(f"view|{ID}|9999999999|0".encode())
            with pytest.raises(NotFound):
                links.verify_view(f"{other_body}.{mac}")

        def it_refuses_a_changed_mac(self, links: LinkService) -> None:
            signature = _signature(links.view_url(ID, epoch=0).url, "/s/")
            body, mac = signature.split(".")
            with pytest.raises(NotFound):
                links.verify_view(f"{body}.{mac[:-1]}x")

        def it_refuses_a_link_signed_with_another_secret(self, links: LinkService) -> None:
            with pytest.raises(NotFound):
                links.verify_view(_forge(f"view|{ID}|9999999999|0", secret=b"y" * 32))

    class WhenTheLinkHasExpired:
        def it_is_not_found(self, links: LinkService, advance: Callable[[timedelta], None]) -> None:
            signature = _signature(links.view_url(ID, epoch=0).url, "/s/")
            advance(timedelta(days=8))
            with pytest.raises(NotFound):
                links.verify_view(signature)

    class WhenTheLinkIsNotShapedLikeOne:
        @pytest.mark.parametrize(
            "signature", ["", "abc", "a.b.c", "!!!.&&&", "a.b.c.d", "abc./def"]
        )
        def it_is_not_found(self, links: LinkService, signature: str) -> None:
            with pytest.raises(NotFound):
                links.verify_view(signature)

    class WhenTheIdInTheLinkIsNotAnId:
        def it_is_not_found(self, links: LinkService, clock: FrozenClock) -> None:
            expires = int((clock.now() + timedelta(days=1)).timestamp())
            with pytest.raises(NotFound):
                links.verify_view(_forge(f"view|shout|{expires}|0"))

    class WhenTheEpochIsNotANumber:
        def it_is_not_found(self, links: LinkService, clock: FrozenClock) -> None:
            expires = int((clock.now() + timedelta(days=1)).timestamp())
            with pytest.raises(NotFound):
                links.verify_view(_forge(f"view|{ID}|{expires}|not-an-epoch"))

    class WhenTheClaimsAreComparedWithAnArtifact:
        def it_accepts_the_link_at_the_current_epoch(
            self, links: LinkService, artifact: Artifact
        ) -> None:
            signature = _signature(
                links.view_url(artifact.id, epoch=artifact.link_epoch).url, "/s/"
            )
            assert links.accepts(links.verify_view(signature), artifact)

        def it_refuses_a_stale_epoch(self, links: LinkService, artifact: Artifact) -> None:
            stale = links.view_url(artifact.id, epoch=artifact.link_epoch + 1)
            assert not links.accepts(links.verify_view(_signature(stale.url, "/s/")), artifact)

    class WhenTheMacIsChecked:
        def it_compares_in_constant_time(
            self, links: LinkService, monkeypatch: pytest.MonkeyPatch
        ) -> None:
            seen: list[object] = []
            original = hmac.compare_digest

            def spy(left: bytes, right: bytes) -> bool:
                seen.append(left)
                return original(left, right)

            monkeypatch.setattr(hmac, "compare_digest", spy)
            signature = _signature(links.view_url(ID, epoch=0).url, "/s/")
            links.verify_view(signature)
            assert seen


class DescribeLinkPolicy:
    class WhenItIsBuiltFromSettings:
        def it_takes_the_link_keys(self, settings: Settings) -> None:
            policy = LinkPolicy.from_settings(settings)
            assert policy.base_url == "http://localhost:8080"
            assert policy.link_ttl == timedelta(days=7)
            assert policy.link_ttl_max == timedelta(days=90)
            assert policy.upload_ttl == timedelta(minutes=15)
            assert policy.upload_ttl_max == timedelta(hours=1)
