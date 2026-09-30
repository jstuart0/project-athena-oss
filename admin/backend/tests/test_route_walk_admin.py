"""The route walk the admin-backend drift tests rely on must see the real
app under the FastAPI this repo pins. On FastAPI 0.141 a flat app.routes
walk finds none of the 66 included routers' routes, and every guard built on
it passed vacuously."""
from __future__ import annotations

import re

from main import app
from shared.route_walk import iter_api_routes

_CONVERTER = re.compile(r"\{([^}:]+):[^}]+\}")


def _walked_operations():
    # OpenAPI drops path converters: {model_name:path} is documented as {model_name}.
    return {
        (method.lower(), _CONVERTER.sub(r"{\1}", w.path))
        for w in iter_api_routes(app)
        if w.route.include_in_schema
        for method in w.methods
    }


def test_walk_is_not_vacuous():
    ops = _walked_operations()
    assert len(ops) > 100
    for named in (("post", "/api/calendar-sources/{source_id}/sync"), ("post", "/api/auth/ws-ticket")):
        assert named in ops


def test_walk_matches_the_openapi_schema_exactly():
    documented = {(m, p) for p, item in app.openapi()["paths"].items() for m in item}
    assert _walked_operations() == documented
