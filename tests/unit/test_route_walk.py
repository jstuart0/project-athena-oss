"""shared.route_walk must see every route on the FastAPI this repo pins,
whatever shape that version gives app.routes. A synthetic app with nested
include_router() prefixes and dependencies, a mounted sub-app, a static
mount and plain routes, checked for an exact result, so the walker can
never go vacuous unnoticed."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fastapi import APIRouter, Depends, FastAPI  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from shared.route_walk import dependency_calls, iter_api_routes, iter_routes  # noqa: E402


def include_guard():
    return True


def endpoint_guard():
    return True


async def _static_app(scope, receive, send):  # an ASGI app with no routes
    raise AssertionError("never called")


def _build():
    inner = APIRouter(prefix="/inner")

    @inner.get("/leaf")
    def inner_leaf(ok=Depends(endpoint_guard)):
        return ok

    outer = APIRouter(prefix="/outer")

    @outer.post("/{item_id}")
    def outer_post(item_id: int):
        return item_id

    outer.include_router(inner, prefix="/mid", dependencies=[Depends(include_guard)])

    app = FastAPI()

    @app.get("/direct")
    def direct():
        return 1

    app.include_router(outer, prefix="/api")
    sub = Starlette(routes=[Route("/ping", lambda r: PlainTextResponse("pong"))])
    app.mount("/sub", sub)
    app.mount("/static", _static_app)
    return app


def test_walk_finds_every_route_with_its_served_path():
    app = _build()
    api = {(m, w.path) for w in iter_api_routes(app) for m in w.methods}
    assert {("GET", "/direct"), ("POST", "/api/outer/{item_id}"), ("GET", "/api/outer/mid/inner/leaf")} <= api
    leaves = {w.path for w in iter_routes(app)}
    assert {"/sub/ping", "/static"} <= leaves
    assert len(api) > 0


def test_paths_match_what_the_app_serves():
    from fastapi.testclient import TestClient

    app = _build()
    client = TestClient(app)
    assert client.get("/api/outer/mid/inner/leaf").status_code == 200
    assert client.post("/api/outer/3").json() == 3
    assert client.get("/sub/ping").text == "pong"


def test_include_and_endpoint_dependencies_are_both_visible():
    app = _build()
    leaf = next(w for w in iter_api_routes(app) if w.path == "/api/outer/mid/inner/leaf")
    calls = dependency_calls(leaf)
    assert include_guard in calls
    assert endpoint_guard in calls
    other = next(w for w in iter_api_routes(app) if w.path == "/api/outer/{item_id}")
    assert include_guard not in dependency_calls(other)


def test_api_routes_match_the_openapi_operations():
    app = _build()
    walked = {(m.lower(), w.path) for w in iter_api_routes(app) if w.route.include_in_schema for m in w.methods}
    schema = app.openapi()["paths"]
    documented = {(method, path) for path, ops in schema.items() for method in ops}
    assert walked == documented
    assert all(isinstance(w.route, APIRoute) for w in iter_api_routes(app))
