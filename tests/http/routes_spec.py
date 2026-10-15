"""The route table (security § 6.2, T3): every route is classified, and each class holds.

The MCP auth provider covers ``/mcp`` only. A route that is added without being classified in
``ROUTE_ACCESS`` fails here, and every classified route is probed without credentials.
"""

from __future__ import annotations

import shutil

import httpx2
import pytest
from starlette.routing import Route

from mcp_drop.app import build_app
from mcp_drop.config import Settings
from mcp_drop.domain import layout
from mcp_drop.services.container import Services
from mcp_drop.web.routes import ROUTE_ACCESS, Access

FORGED = "dmlld3xrN20.AAAAAAAAAAAAAAAAAAAAAA"


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
            ("POST", "/mcp", 401),
            ("GET", f"/s/{FORGED}/", 404),
            ("PUT", f"/u/{FORGED}?filename=a.pdf", 404),
            ("GET", "/a/k7m2q9x4tz3wbn6r/", 404),
        ],
    )
    def it_refuses_a_caller_without_credentials(
        self, http: httpx2.Client, method: str, path: str, status: int
    ) -> None:
        assert http.request(method, path).status_code == status

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
