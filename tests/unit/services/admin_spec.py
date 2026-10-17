"""The dashboard's login (spec § 7.6, ADR 0008): the password hash and the stateless session."""

from __future__ import annotations

from datetime import timedelta

import pytest

from mcp_vitrine.domain.errors import Invalid
from mcp_vitrine.services.admin import SESSION_TTL, AdminLogin, hash_password
from tests.conftest import ADMIN_HASH, ADMIN_PASSWORD, SECRET, FrozenClock


@pytest.fixture
def login(clock: FrozenClock) -> AdminLogin:
    """The login of the shared password, under the shared secret."""
    return AdminLogin(ADMIN_HASH, SECRET.encode(), clock)


class DescribeThePasswordHash:
    class WhenAPasswordIsHashed:
        def it_never_holds_the_password(self) -> None:
            assert ADMIN_PASSWORD not in ADMIN_HASH

        def it_names_scrypt_and_its_parameters(self) -> None:
            assert ADMIN_HASH.split("$")[:4] == ["scrypt", str(2**15), "8", "1"]

    class WhenThePasswordIsTooShort:
        def it_refuses_it(self) -> None:
            with pytest.raises(Invalid, match="12 to"):
                hash_password("short")


class DescribeVerifying:
    class WhenThePasswordIsRight:
        def it_accepts_it(self, login: AdminLogin) -> None:
            assert login.verify(ADMIN_PASSWORD)

    class WhenThePasswordIsWrong:
        def it_refuses_it(self, login: AdminLogin) -> None:
            assert not login.verify(ADMIN_PASSWORD + "!")

    class WhenThePasswordIsOverlong:
        def it_refuses_it_without_hashing(self, login: AdminLogin) -> None:
            assert not login.verify("x" * 2000)


class DescribeTheSession:
    class WhenItWasIssued:
        def it_verifies(self, login: AdminLogin) -> None:
            assert login.session(login.issue()) is not None

        def it_carries_a_csrf_token(self, login: AdminLogin) -> None:
            session = login.session(login.issue())
            assert session is not None
            assert len(session.csrf) >= 32

    class WhenTwoAreIssued:
        def it_gives_each_its_own_csrf_token(self, login: AdminLogin) -> None:
            first, second = login.session(login.issue()), login.session(login.issue())
            assert first is not None
            assert second is not None
            assert first.csrf != second.csrf

    class WhenItIsTamperedWith:
        def it_does_not_verify(self, login: AdminLogin) -> None:
            body, _, mac = login.issue().rpartition(".")
            assert login.session(f"{body}.{mac[::-1]}") is None

    class WhenItIsOlderThanTwelveHours:
        def it_does_not_verify(self, login: AdminLogin, clock: FrozenClock) -> None:
            value = login.issue()
            clock.advance(SESSION_TTL + timedelta(seconds=1))
            assert login.session(value) is None

    class WhenThePasswordChanges:
        def it_ends_the_session(self, login: AdminLogin, clock: FrozenClock) -> None:
            value = login.issue()
            changed = AdminLogin(hash_password("another long password"), SECRET.encode(), clock)
            assert changed.session(value) is None

    class WhenTheSecretChanges:
        def it_ends_the_session(self, login: AdminLogin, clock: FrozenClock) -> None:
            value = login.issue()
            rotated = AdminLogin(ADMIN_HASH, b"another secret of at least 32 bytes", clock)
            assert rotated.session(value) is None

    @pytest.mark.parametrize("value", [None, "", "v1", "v1.x.y.z", "a" * 500])
    def it_refuses_what_it_never_issued(self, login: AdminLogin, value: str | None) -> None:
        assert login.session(value) is None

    def it_refuses_a_cookie_that_is_not_ascii(self, login: AdminLogin) -> None:
        body, _, _ = login.issue().rpartition(".")
        assert login.session(f"{body}.é") is None
