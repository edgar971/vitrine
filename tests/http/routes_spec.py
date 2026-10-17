"""The route table (security § 6.2, T3): every route is classified, and each class holds.

The MCP auth provider covers ``/mcp`` only. A route that is added without being classified in
``ROUTE_ACCESS`` fails here, and every classified route is probed without credentials.
"""

from __future__ import annotations

import shutil

import httpx2
import pytest
from starlette.routing import Route

from mcp_vitrine.app import build_app
from mcp_vitrine.config import Settings
from mcp_vitrine.domain import layout
from mcp_vitrine.services.container import Services
from mcp_vitrine.web.routes import ROUTE_ACCESS, Access
from tests.conftest import ADMIN_HASH

FORGED = "dmlld3xrN20.AAAAAAAAAAAAAAAAAAAAAA"
DASHBOARD_ACTIONS = ("open", "links", "revoke", "meta", "private", "delete")


@pytest.fixture
def overrides() -> dict[str, str]:
    """The dashboard on, so its routes are in the table too."""
    return {"VITRINE_ADMIN_PASSWORD": ADMIN_HASH}


@pytest.fixture
def route_names(settings: Settings, services: Services) -> set[str]:
    """The names of every route the assembled app serves."""
    return {
        route.name for route in build_app(settings, services).routes if isinstance(route, Route)
    }


class DescribeTheRouteTable:
    class WhenTheAppIsAssembled:
        def it_classifies_every_route(self, route_names: set[str]) -> None:
            assert route_names <= set(ROUTE_ACCESS)

        def it_classifies_no_route_that_is_gone(self, route_names: set[str]) -> None:
            assert set(ROUTE_ACCESS) <= route_names

        def it_serves_only_mounted_routes(self, settings: Settings, services: Services) -> None:
            routes = build_app(settings, services).routes
            assert all(isinstance(route, Route) for route in routes)

    @pytest.mark.parametrize(
        ("method", "path", "status"),
        [
            ("POST", "/api/artifacts", 401),
            ("PUT", "/api/artifacts/k7m2q9x4tz3wbn6r", 401),
            ("GET", "/api/artifacts", 401),
            ("GET", "/api/artifacts/k7m2q9x4tz3wbn6r", 401),
            ("DELETE", "/api/artifacts/k7m2q9x4tz3wbn6r", 401),
            ("PATCH", "/api/artifacts/k7m2q9x4tz3wbn6r", 401),
            ("POST", "/api/artifacts/k7m2q9x4tz3wbn6r/links", 401),
            ("POST", "/api/artifacts/k7m2q9x4tz3wbn6r/revoke", 401),
            ("POST", "/mcp", 401),
            ("GET", f"/s/{FORGED}/", 404),
            ("PUT", f"/u/{FORGED}?filename=a.pdf", 404),
            ("GET", "/a/k7m2q9x4tz3wbn6r/", 404),
            ("GET", "/_vitrine/jsx/unknown/react.js", 404),
            ("GET", "/_admin/static/unknown.css", 404),
            ("POST", "/_admin/login", 403),
            ("POST", "/_admin/logout", 403),
            *(("POST", f"/_admin/a/k7m2q9x4tz3wbn6r/{name}", 403) for name in DASHBOARD_ACTIONS),
        ],
    )
    def it_refuses_a_caller_without_credentials(
        self, http: httpx2.Client, method: str, path: str, status: int
    ) -> None:
        assert http.request(method, path).status_code == status

    @pytest.mark.parametrize(
        "path",
        [
            "/_admin/",
            "/_admin/audit",
            "/_admin/a/k7m2q9x4tz3wbn6r",
            "/_admin/a/k7m2q9x4tz3wbn6r/confirm/delete",
        ],
    )
    def it_sends_a_browser_without_a_session_to_the_login(
        self, http: httpx2.Client, path: str
    ) -> None:
        assert http.get(path).headers["location"] == "/_admin/login"

    @pytest.mark.parametrize(
        "path", [f"/_admin/a/k7m2q9x4tz3wbn6r/{name}" for name in DASHBOARD_ACTIONS]
    )
    def it_refuses_a_dashboard_change_without_a_session(
        self, http: httpx2.Client, base: str, path: str
    ) -> None:
        assert http.post(path, headers={"Origin": base}).headers["location"] == "/_admin/login"

    def it_names_the_classes_it_holds(self) -> None:
        assert set(ROUTE_ACCESS.values()) == set(Access)


class DescribeTheHealthCheck:
    class WhenTheDataDirIsWritable:
        def it_answers_ok(self, http: httpx2.Client) -> None:
            response = http.get("/healthz")
            assert (response.status_code, response.json()) == (200, {"ok": True})

    class WhenTheDataDirIsBroken:
        def it_answers_503(self, http: httpx2.Client, settings: Settings) -> None:
            shutil.rmtree(settings.data_dir / layout.TMP_DIR)
            assert http.get("/healthz").status_code == 503
