"""Enumerate every concrete route a FastAPI/Starlette app serves.

A flat walk of `app.routes` is version-dependent. FastAPI before ~0.140
copied every included router's routes into `app.routes` as full-path
`APIRoute`s. FastAPI 0.141 (the version this repo pins) keeps one wrapper
object per `include_router()` call instead, so a flat walk of an app built
from included routers finds none of their routes. A drift test built on it
passes vacuously.

`iter_routes` recurses through both shapes and through mounts, and yields
each leaf route with the full path it's served under and the dependencies
added by `include_router(dependencies=...)` on the way down (on the old
shape those are already merged into the copied route, so they come back
empty there).

Callers should still assert a floor and a named member on what they find:
this walker can't know about a routing shape that doesn't exist yet.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator


@dataclass(frozen=True)
class WalkedRoute:
    path: str
    route: Any
    include_dependencies: tuple = ()

    @property
    def methods(self) -> frozenset:
        return frozenset(getattr(self.route, "methods", None) or ())


def iter_routes(app_or_router: Any) -> Iterator[WalkedRoute]:
    """Every leaf route of an app, router or mount, depth-first in
    registration order. Mounts with no routes of their own (static files, a
    non-Starlette ASGI app) are yielded as leaves."""
    yield from _walk(getattr(app_or_router, "routes", ()) or (), "", ())


def _walk(routes: Iterable[Any], prefix: str, deps: tuple) -> Iterator[WalkedRoute]:
    for route in routes:
        context = getattr(route, "include_context", None)
        included = getattr(context, "included_router", None)
        if included is not None:
            yield from _walk(
                included.routes,
                prefix + (getattr(context, "prefix", "") or ""),
                deps + tuple(getattr(context, "dependencies", None) or ()),
            )
            continue
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _walk(original.routes, prefix, deps)
            continue
        children = getattr(route, "routes", None)
        if children:
            yield from _walk(children, prefix + (getattr(route, "path", "") or ""), deps)
            continue
        yield WalkedRoute(prefix + (getattr(route, "path", "") or ""), route, deps)


def iter_api_routes(app_or_router: Any) -> Iterator[WalkedRoute]:
    """Only FastAPI `APIRoute`s (HTTP endpoints with a dependant)."""
    from fastapi.routing import APIRoute

    for walked in iter_routes(app_or_router):
        if isinstance(walked.route, APIRoute):
            yield walked


def dependency_calls(walked: WalkedRoute) -> list[Callable]:
    """Every callable in the route's dependency tree, plus the dependencies
    its include_router() calls added. Used to prove a guard is wired."""
    calls: list[Callable] = []
    stack = [getattr(walked.route, "dependant", None)]
    while stack:
        dependant = stack.pop()
        if dependant is None:
            continue
        if dependant.call is not None:
            calls.append(dependant.call)
        stack.extend(dependant.dependencies)
    for depends in walked.include_dependencies:
        dependency = getattr(depends, "dependency", None)
        if dependency is not None:
            calls.append(dependency)
    return calls
