"""The operator dashboard over real HTTP (spec § 7.6, ADR 0008; security T24, T25).

It signs in with the operator password only, keeps the session in a strict cookie scoped to
``/_admin``, refuses a change from another origin or without the session's CSRF token, escapes
what agents wrote, and offers no action that makes anything public.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

import httpx2
import pytest

from mcp_vitrine.domain.models import (
    ArtifactDraft,
    GateDecision,
    PublishedArtifact,
    Visibility,
)
from mcp_vitrine.services.admin import AdminLogin
from mcp_vitrine.services.audit import AuditEventName
from mcp_vitrine.services.container import Services
from mcp_vitrine.web.dashboard import PASSWORD_CHECKS
from tests.conftest import ADMIN_HASH, ADMIN_PASSWORD
from tests.http.conftest import CLIENT, FULL_TOKEN

SCRIPT_TITLE = "<script>alert(1)</script> Weekly metrics"
CSRF = re.compile(r'name="csrf" value="([^"]+)"')


@pytest.fixture
def overrides() -> dict[str, str]:
    """The dashboard on, with its password."""
    return {"VITRINE_ADMIN_PASSWORD": ADMIN_HASH}


@pytest.fixture
def origin(base: str) -> dict[str, str]:
    """The header every browser sends on a form post from vitrine's own pages."""
    return {"Origin": base}


@pytest.fixture
def operator(base: str, origin: dict[str, str]) -> Iterator[httpx2.Client]:
    """A browser signed in to the dashboard: it holds the session cookie."""
    with httpx2.Client(base_url=base) as client:
        response = client.post("/_admin/login", data={"password": ADMIN_PASSWORD}, headers=origin)
        assert response.status_code == 303
        yield client


@pytest.fixture
def csrf(operator: httpx2.Client) -> str:
    """The CSRF token the signed-in pages carry."""
    match = CSRF.search(operator.get("/_admin/").text)
    assert match is not None
    return match.group(1)


type Act = Callable[..., httpx2.Response]


@pytest.fixture
def act(operator: httpx2.Client, csrf: str, origin: dict[str, str]) -> Act:
    """Post a form from the signed-in browser, as its own pages post them."""

    def post(path: str, **form: str) -> httpx2.Response:
        return operator.post(path, data={"csrf": csrf, **form}, headers=origin)

    return post


@pytest.fixture
def public(services: Services, published: PublishedArtifact) -> PublishedArtifact:
    """The published artifact, made public through an approving gate."""
    services.artifacts.set_visibility(
        published.id, Visibility.PUBLIC, client=CLIENT, gate=GateDecision.APPROVED
    )
    return published


def _visibility(services: Services, artifact_id: str) -> Visibility:
    """What the store holds now."""
    return services.artifacts.get(artifact_id).artifact.visibility


class DescribeTheDashboard:
    class WhenNoAdminPasswordIsSet:
        @pytest.fixture
        def overrides(self) -> dict[str, str]:
            """The dashboard off."""
            return {}

        @pytest.mark.parametrize("path", ["/_admin/", "/_admin/login", "/_admin/static/admin.css"])
        def it_serves_no_admin_route(self, http: httpx2.Client, path: str) -> None:
            assert http.get(path).status_code == 404

    class WhenNobodyIsSignedIn:
        def it_sends_the_browser_to_the_login(self, http: httpx2.Client) -> None:
            response = http.get("/_admin/")
            assert (response.status_code, response.headers["location"]) == (303, "/_admin/login")

    class WhenABearerTokenIsSent:
        def it_does_not_log_in(self, http: httpx2.Client) -> None:
            response = http.get("/_admin/", headers={"Authorization": f"Bearer {FULL_TOKEN}"})
            assert response.headers["location"] == "/_admin/login"

        def it_is_not_taken_as_the_password(
            self, http: httpx2.Client, origin: dict[str, str]
        ) -> None:
            response = http.post("/_admin/login", data={"password": FULL_TOKEN}, headers=origin)
            assert response.status_code == 401

    class WhenThePasswordIsRight:
        @pytest.fixture
        def answer(self, http: httpx2.Client, origin: dict[str, str]) -> httpx2.Response:
            """The login's answer."""
            return http.post("/_admin/login", data={"password": ADMIN_PASSWORD}, headers=origin)

        def it_sends_the_browser_to_the_list(self, answer: httpx2.Response) -> None:
            assert (answer.status_code, answer.headers["location"]) == (303, "/_admin/")

        def it_sets_a_strict_http_only_cookie_scoped_to_the_dashboard(
            self, answer: httpx2.Response
        ) -> None:
            cookie = answer.headers["set-cookie"].lower()
            assert {"httponly", "samesite=strict", "path=/_admin"} <= {
                part.strip() for part in cookie.split(";")
            }

    class WhenThePasswordIsWrong:
        @pytest.fixture
        def overrides(self) -> dict[str, str]:
            """One wrong password a minute."""
            return {"VITRINE_ADMIN_PASSWORD": ADMIN_HASH, "VITRINE_RATE_ADMIN_LOGIN": "1/m"}

        def it_answers_401_with_one_generic_message(
            self, http: httpx2.Client, origin: dict[str, str]
        ) -> None:
            response = http.post("/_admin/login", data={"password": "wrong"}, headers=origin)
            assert (response.status_code, "That password didn&#x27;t work." in response.text) == (
                401,
                True,
            )

        def it_counts_against_rate_admin_login(
            self, http: httpx2.Client, origin: dict[str, str]
        ) -> None:
            http.post("/_admin/login", data={"password": "wrong"}, headers=origin)
            response = http.post("/_admin/login", data={"password": ADMIN_PASSWORD}, headers=origin)
            assert response.status_code == 429

        def it_does_not_count_a_right_one(
            self, http: httpx2.Client, origin: dict[str, str]
        ) -> None:
            http.post("/_admin/login", data={"password": ADMIN_PASSWORD}, headers=origin)
            response = http.post("/_admin/login", data={"password": ADMIN_PASSWORD}, headers=origin)
            assert response.status_code == 303

    class WhenManyLoginsArriveAtOnce:
        @pytest.fixture
        def overrides(self) -> dict[str, str]:
            """A bucket wide enough that only the cap on checks holds them back."""
            return {"VITRINE_ADMIN_PASSWORD": ADMIN_HASH, "VITRINE_RATE_ADMIN_LOGIN": "50/m"}

        @pytest.fixture
        def overlapping(
            self, base: str, origin: dict[str, str], monkeypatch: pytest.MonkeyPatch
        ) -> int:
            """The most password checks that ran at the same time across 8 parallel logins."""
            running = 0
            most = 0
            guard = threading.Lock()

            def slow(_: AdminLogin, __: str) -> bool:
                nonlocal running, most
                with guard:
                    running += 1
                    most = max(most, running)
                time.sleep(0.1)
                with guard:
                    running -= 1
                return False

            monkeypatch.setattr(AdminLogin, "verify", slow)

            def attempt(_: int) -> int:
                with httpx2.Client(base_url=base) as client:
                    response = client.post(
                        "/_admin/login", data={"password": "wrong"}, headers=origin
                    )
                    return response.status_code

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(attempt, range(8)))
            return most

        def it_runs_at_most_password_checks_at_once(self, overlapping: int) -> None:
            assert overlapping <= PASSWORD_CHECKS

    class WhenALoginComesFromAnotherOrigin:
        def it_answers_403(self, http: httpx2.Client) -> None:
            response = http.post(
                "/_admin/login",
                data={"password": ADMIN_PASSWORD},
                headers={"Origin": "https://evil.example"},
            )
            assert response.status_code == 403

    class WhenAPostHasAForeignOrigin:
        def it_answers_403(
            self,
            operator: httpx2.Client,
            csrf: str,
            published: PublishedArtifact,
        ) -> None:
            response = operator.post(
                f"/_admin/a/{published.id}/delete",
                data={"csrf": csrf},
                headers={"Origin": "https://evil.example"},
            )
            assert response.status_code == 403

        def it_changes_nothing(
            self,
            operator: httpx2.Client,
            csrf: str,
            published: PublishedArtifact,
            services: Services,
        ) -> None:
            operator.post(
                f"/_admin/a/{published.id}/delete",
                data={"csrf": csrf},
                headers={"Origin": "https://evil.example"},
            )
            assert services.artifacts.get(published.id).artifact.id == published.id

    class WhenAPostIsCrossSite:
        def it_answers_403(
            self,
            operator: httpx2.Client,
            csrf: str,
            published: PublishedArtifact,
            origin: dict[str, str],
        ) -> None:
            response = operator.post(
                f"/_admin/a/{published.id}/delete",
                data={"csrf": csrf},
                headers={**origin, "Sec-Fetch-Site": "cross-site"},
            )
            assert response.status_code == 403

    class WhenTheCsrfTokenIsMissing:
        def it_answers_403(
            self, operator: httpx2.Client, published: PublishedArtifact, origin: dict[str, str]
        ) -> None:
            response = operator.post(f"/_admin/a/{published.id}/delete", headers=origin)
            assert response.status_code == 403

    class WhenTheCsrfTokenIsAnotherSessions:
        def it_answers_403(
            self,
            operator: httpx2.Client,
            published: PublishedArtifact,
            origin: dict[str, str],
            base: str,
        ) -> None:
            with httpx2.Client(base_url=base) as other:
                other.post("/_admin/login", data={"password": ADMIN_PASSWORD}, headers=origin)
                match = CSRF.search(other.get("/_admin/").text)
                assert match is not None
            response = operator.post(
                f"/_admin/a/{published.id}/delete", data={"csrf": match.group(1)}, headers=origin
            )
            assert response.status_code == 403

    class WhenAPostCarriesNoSession:
        def it_sends_the_browser_to_the_login(
            self, http: httpx2.Client, published: PublishedArtifact, origin: dict[str, str]
        ) -> None:
            response = http.post(f"/_admin/a/{published.id}/delete", headers=origin)
            assert response.headers["location"] == "/_admin/login"

    class WhenATitleHoldsScript:
        @pytest.fixture
        def scripted(self, services: Services) -> PublishedArtifact:
            """An artifact whose agent-written title, tag and project try to run script."""
            return services.artifacts.create(
                ArtifactDraft(
                    title=SCRIPT_TITLE,
                    files={"index.html": b"<p>x</p>"},
                    tags=('"><img src=x onerror=alert(1)>',),
                ),
                client=CLIENT,
            )

        def it_escapes_it(self, operator: httpx2.Client, scripted: PublishedArtifact) -> None:
            page = operator.get("/_admin/").text
            assert "<script>alert(1)</script>" not in page

        def it_escapes_it_on_the_artifact_page(
            self, operator: httpx2.Client, scripted: PublishedArtifact
        ) -> None:
            page = operator.get(f"/_admin/a/{scripted.id}").text
            assert ("<script>alert" in page, "<img src=x" in page) == (False, False)

    class WhenAPageIsServed:
        @pytest.fixture
        def headers(self, operator: httpx2.Client) -> httpx2.Headers:
            """The headers of the list."""
            return operator.get("/_admin/").headers

        def it_allows_no_inline_script_or_style(self, headers: httpx2.Headers) -> None:
            assert headers["content-security-policy"].startswith("default-src 'self'")

        def it_cannot_be_framed(self, headers: httpx2.Headers) -> None:
            assert "frame-ancestors 'none'" in headers["content-security-policy"]

        def it_is_never_cached(self, headers: httpx2.Headers) -> None:
            assert headers["cache-control"] == "private, no-store"

    @pytest.mark.parametrize(
        ("action", "form"),
        [
            ("open", {}),
            ("links", {"expires_in": "7d", "generate": "1"}),
            ("meta", {"title": "public", "tags": "public", "project": "public"}),
            ("revoke", {}),
            ("private", {}),
            ("public", {}),
            ("visibility", {"visibility": "public"}),
        ],
    )
    def it_offers_no_action_that_makes_an_artifact_public(
        self,
        act: Act,
        published: PublishedArtifact,
        services: Services,
        action: str,
        form: dict[str, str],
    ) -> None:
        act(f"/_admin/a/{published.id}/{action}", **form)
        assert _visibility(services, published.id) is Visibility.PRIVATE


class DescribeDashboardActions:
    class WhenAPublicArtifactIsMadePrivate:
        @pytest.fixture
        def answer(self, act: Act, public: PublishedArtifact) -> httpx2.Response:
            """The answer to the confirmed form."""
            return act(f"/_admin/a/{public.id}/private")

        def it_takes_down_the_public_url(
            self, answer: httpx2.Response, http: httpx2.Client, public: PublishedArtifact
        ) -> None:
            assert http.get(f"/a/{public.id}/").status_code == 404

        def it_audits_the_change_as_the_operator(
            self, answer: httpx2.Response, services: Services, public: PublishedArtifact
        ) -> None:
            event = services.artifacts.history(public.id)[0]
            assert (event.event, event.client, event.gate) == (
                AuditEventName.VISIBILITY_CHANGED,
                "@admin",
                GateDecision.NOT_REQUIRED,
            )

    class WhenALinkIsMinted:
        @pytest.fixture
        def answer(self, act: Act, published: PublishedArtifact) -> httpx2.Response:
            """The page that answers the form."""
            return act(f"/_admin/a/{published.id}/links", label="legal", expires_in="1d")

        def it_shows_the_link_once(self, answer: httpx2.Response) -> None:
            assert (answer.status_code, 'id="minted-url"' in answer.text) == (201, True)

        def it_opens_the_artifact(self, answer: httpx2.Response, http: httpx2.Client) -> None:
            match = re.search(r'id="minted-url"[^>]*value="([^"]+)"', answer.text)
            assert match is not None
            assert http.get(match.group(1)).status_code == 200

        def it_does_not_show_it_again(
            self, answer: httpx2.Response, operator: httpx2.Client, published: PublishedArtifact
        ) -> None:
            assert "minted-url" not in operator.get(f"/_admin/a/{published.id}").text

        def it_audits_it_as_the_operator(
            self, answer: httpx2.Response, services: Services, published: PublishedArtifact
        ) -> None:
            event = services.artifacts.history(published.id)[0]
            assert (event.event, event.client) == (AuditEventName.LINK_ISSUED, "@admin")

    class WhenAPasscodeIsGenerated:
        def it_shows_it_once(self, act: Act, published: PublishedArtifact) -> None:
            page = act(f"/_admin/a/{published.id}/links", expires_in="1d", generate="1").text
            assert re.search(r'class="secret">[0-9a-z]{5}-[0-9a-z]{5}<', page) is not None

    class WhenALinkIsRevoked:
        def it_stops_working(
            self,
            act: Act,
            services: Services,
            published: PublishedArtifact,
            http: httpx2.Client,
        ) -> None:
            link = services.artifacts.link(published.id, client=CLIENT, label="finance")
            act(f"/_admin/a/{published.id}/revoke", link=link.lid or "")
            assert http.get(link.url).status_code == 404

    class WhenTheDetailsAreSaved:
        def it_changes_title_tags_and_project(
            self, act: Act, services: Services, published: PublishedArtifact
        ) -> None:
            act(f"/_admin/a/{published.id}/meta", title="Energy 2", tags="a, b", project="home")
            meta = services.artifacts.get(published.id).artifact
            assert (meta.title, meta.tags, meta.project) == ("Energy 2", ("a", "b"), "home")

    class WhenTheDetailsAreInvalid:
        def it_answers_400_and_keeps_them(
            self, act: Act, services: Services, published: PublishedArtifact
        ) -> None:
            response = act(f"/_admin/a/{published.id}/meta", title="Energy", project="Not One")
            project = services.artifacts.get(published.id).artifact.project
            assert (response.status_code, project) == (400, None)

    class WhenAnArtifactIsDeleted:
        def it_is_gone(
            self, act: Act, operator: httpx2.Client, published: PublishedArtifact
        ) -> None:
            act(f"/_admin/a/{published.id}/delete")
            assert operator.get(f"/_admin/a/{published.id}").status_code == 404

    class WhenTheOperatorSignsOut:
        def it_drops_the_cookie(self, act: Act, operator: httpx2.Client) -> None:
            act("/_admin/logout")
            assert operator.get("/_admin/").headers["location"] == "/_admin/login"

    class WhenTheAuditLogIsOpened:
        def it_lists_the_events(
            self, operator: httpx2.Client, published: PublishedArtifact
        ) -> None:
            page = operator.get("/_admin/audit", params={"artifact": published.id}).text
            assert published.id in page

    class WhenAnUnknownArtifactIsAskedFor:
        def it_answers_404(self, operator: httpx2.Client) -> None:
            assert operator.get("/_admin/a/k7m2q9x4tz3wbn6r").status_code == 404


class DescribeDashboardPages:
    class WhenSomeoneSignedInOpensTheLogin:
        def it_sends_them_to_the_list(self, operator: httpx2.Client) -> None:
            assert operator.get("/_admin/login").headers["location"] == "/_admin/"

    class WhenADeleteIsConfirmed:
        def it_names_what_will_go(
            self, operator: httpx2.Client, published: PublishedArtifact
        ) -> None:
            page = operator.get(f"/_admin/a/{published.id}/confirm/delete").text
            assert "This can't be undone." in page

    class WhenMakingAPrivateArtifactPrivateIsAskedFor:
        def it_goes_back_to_the_artifact(
            self, operator: httpx2.Client, published: PublishedArtifact
        ) -> None:
            response = operator.get(f"/_admin/a/{published.id}/confirm/private")
            assert response.headers["location"] == f"/_admin/a/{published.id}"

    class WhenAnUnknownStepIsAskedFor:
        def it_answers_404(self, operator: httpx2.Client, published: PublishedArtifact) -> None:
            assert operator.get(f"/_admin/a/{published.id}/confirm/publish").status_code == 404

    class WhenAPublicArtifactIsOpened:
        def it_goes_to_the_public_url(self, act: Act, public: PublishedArtifact, base: str) -> None:
            response = act(f"/_admin/a/{public.id}/open")
            assert response.headers["location"] == f"{base}/a/{public.id}/"

    class WhenAPrivateArtifactIsOpened:
        def it_goes_to_a_private_link(
            self, act: Act, published: PublishedArtifact, base: str
        ) -> None:
            response = act(f"/_admin/a/{published.id}/open")
            assert response.headers["location"].startswith(f"{base}/s/")

    class WhenALinkIsAskedForOnAPublicArtifact:
        def it_answers_400(self, act: Act, public: PublishedArtifact) -> None:
            response = act(f"/_admin/a/{public.id}/links", label="x", expires_in="1d")
            assert response.status_code == 400

    class WhenALinkHasNoExpiryChoice:
        def it_answers_400(self, act: Act, published: PublishedArtifact) -> None:
            response = act(f"/_admin/a/{published.id}/links", expires_in="never")
            assert response.status_code == 400

    class WhenAMalformedLinkIsRevoked:
        def it_answers_404(self, act: Act, published: PublishedArtifact) -> None:
            assert act(f"/_admin/a/{published.id}/revoke", link="../x").status_code == 404

    class WhenTheAuditLogIsFilteredByEvent:
        def it_shows_only_that_event(
            self, operator: httpx2.Client, services: Services, published: PublishedArtifact
        ) -> None:
            services.artifacts.link(published.id, client=CLIENT, label="finance")
            page = operator.get("/_admin/audit", params={"event": "link_issued"}).text
            assert ("e-link" in page, "e-version" in page) == (True, False)

    class WhenTheListIsFiltered:
        def it_shows_only_what_matches(
            self, operator: httpx2.Client, services: Services, public: PublishedArtifact
        ) -> None:
            services.artifacts.create(
                ArtifactDraft(title="Other", files={"a.txt": b"a"}), client=CLIENT
            )
            page = operator.get("/_admin/", params={"visibility": "public"}).text
            assert (public.id in page, ">Other<" in page) == (True, False)

    class WhenAFontIsAskedFor:
        def it_is_served_as_woff2(self, http: httpx2.Client) -> None:
            response = http.get("/_admin/static/fonts/instrument-sans-latin-400-normal.woff2")
            assert response.headers["content-type"] == "font/woff2"

    class WhenAStaticPathClimbs:
        def it_answers_404(self, http: httpx2.Client) -> None:
            assert http.get("/_admin/static/fonts/%2e%2e/admin.css").status_code == 404

    class WhenTheCsrfTokenIsNotAscii:
        def it_answers_403(
            self, operator: httpx2.Client, published: PublishedArtifact, origin: dict[str, str]
        ) -> None:
            response = operator.post(
                f"/_admin/a/{published.id}/delete", data={"csrf": "é"}, headers=origin
            )
            assert response.status_code == 403
