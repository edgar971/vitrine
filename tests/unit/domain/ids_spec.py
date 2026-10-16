"""Identifiers: artifact ids, client references and nonces (spec § 3, § 4.5, § 7.3)."""

from __future__ import annotations

import pytest

from mcp_vitrine.domain.errors import Invalid
from mcp_vitrine.domain.ids import (
    ALPHABET,
    CLIENT_REF_MAX,
    ID_LENGTH,
    is_valid_id,
    new_id,
    new_nonce,
    validate_client_ref,
)


class DescribeArtifactIds:
    class WhenAnIdIsDrawn:
        def it_is_sixteen_crockford_characters(self) -> None:
            assert len(new_id()) == ID_LENGTH

        def it_uses_only_the_crockford_alphabet(self) -> None:
            assert set(new_id()) <= set(ALPHABET)

        def it_leaves_out_the_letters_that_confuse_readers(self) -> None:
            assert not set("iloUILO") & set(new_id())

        def it_looks_random_from_one_draw_to_the_next(self) -> None:
            assert len({new_id() for _ in range(1000)}) == 1000


class DescribeIdValidation:
    class WhenTheIdHasOneOfTheDocumentedShapes:
        def it_accepts_a_fresh_id(self) -> None:
            assert is_valid_id(new_id())

    class WhenTheIdIsTooShortOrTooLong:
        def it_rejects_it(self) -> None:
            assert not is_valid_id("k7m2q9x4tz3wbn6")
            assert not is_valid_id("k7m2q9x4tz3wbn6rr")

    class WhenTheIdUsesACrockfordExcludedLetter:
        def it_rejects_it(self) -> None:
            assert not is_valid_id("k7m2q9x4tz3wbn6i")

    class WhenTheIdIsNotLowercase:
        def it_rejects_it(self) -> None:
            assert not is_valid_id("K7M2Q9X4TZ3WBN6R")

    class WhenTheIdIsNotAscii:
        def it_rejects_it(self) -> None:
            assert not is_valid_id("k7m2q9x4tz3wbn6é")


class DescribeUploadNonces:
    class WhenATokenIsDrawn:
        def it_needs_no_padding(self) -> None:
            assert "=" not in new_nonce()

        def it_is_url_safe(self) -> None:
            assert new_nonce().replace("-", "").replace("_", "").isalnum()


class DescribeClientRefs:
    class WhenTheRefUsesTheAllowedCharacters:
        def it_accepts_it(self) -> None:
            assert validate_client_ref("weekly-energy-2026-09") == "weekly-energy-2026-09"

        def it_accepts_the_punctuation_spec_4_5_lists(self) -> None:
            assert validate_client_ref("a.b_c:d-1") == "a.b_c:d-1"

    class WhenTheRefIsEmpty:
        def it_refuses_it(self) -> None:
            with pytest.raises(Invalid):
                validate_client_ref("")

    class WhenTheRefIsTooLong:
        def it_refuses_it(self) -> None:
            with pytest.raises(Invalid):
                validate_client_ref("a" * (CLIENT_REF_MAX + 1))

    class WhenTheRefHoldsASpaceOrASlash:
        def it_refuses_it(self) -> None:
            with pytest.raises(Invalid):
                validate_client_ref("weekly energy")
            with pytest.raises(Invalid):
                validate_client_ref("../escape")
