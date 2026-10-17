"""Link passcodes (spec § 3, ADR 0006): generated or supplied, kept as an scrypt hash only."""

from __future__ import annotations

import re

import pytest

from mcp_vitrine.domain.errors import Invalid
from mcp_vitrine.domain.models import Passcode
from mcp_vitrine.services import passcodes

SHOWN = re.compile(r"^[0-9a-hjkmnp-tv-z]{5}-[0-9a-hjkmnp-tv-z]{5}$")


class DescribeAGeneratedPasscode:
    def it_is_shown_as_two_groups_of_five(self) -> None:
        assert SHOWN.fullmatch(passcodes.generate())

    def it_differs_each_time(self) -> None:
        assert passcodes.generate() != passcodes.generate()


class DescribeTheHash:
    @pytest.fixture
    def record(self) -> Passcode:
        """The record of one passcode."""
        return passcodes.digest("abcde-fghjk")

    def it_never_holds_the_passcode(self, record: Passcode) -> None:
        assert "abcde" not in record.model_dump_json()

    def it_uses_the_documented_scrypt_parameters(self, record: Passcode) -> None:
        assert (record.n, record.r, record.p) == (2**15, 8, 1)

    def it_salts_each_passcode(self, record: Passcode) -> None:
        assert passcodes.digest("abcde-fghjk").hash != record.hash

    class WhenTheRightPasscodeIsGiven:
        def it_matches(self, record: Passcode) -> None:
            assert passcodes.matches("abcde-fghjk", record)

        def it_ignores_case_and_hyphens(self, record: Passcode) -> None:
            assert passcodes.matches("ABCDEFGHJK", record)

    class WhenAnotherPasscodeIsGiven:
        def it_does_not_match(self, record: Passcode) -> None:
            assert not passcodes.matches("abcde-fghjm", record)

    class WhenAnOverlongPasscodeIsGiven:
        def it_does_not_match_without_hashing_it(self, record: Passcode) -> None:
            assert not passcodes.matches("a" * 10_000, record)


class DescribeASuppliedPasscode:
    class WhenItIsTooShort:
        def it_is_refused_without_repeating_it(self) -> None:
            with pytest.raises(Invalid) as refusal:
                passcodes.check_supplied("secret7")
            assert "secret7" not in str(refusal.value)

    class WhenItIsTooLong:
        def it_is_refused(self) -> None:
            with pytest.raises(Invalid):
                passcodes.check_supplied("x" * 129)

    class WhenItFits:
        def it_is_accepted(self) -> None:
            passcodes.check_supplied("correct horse")
