"""Smoke tests for /api/health/* endpoints — 200 + valid JSON shape."""

from __future__ import annotations

import pytest


_SMOKE_ENDPOINTS = [
    "/api/health/tiles",
    "/api/health/dau-mau",
    "/api/health/newcomer-funnel",
    "/api/health/gini",
    "/api/health/mod-workload",
]


@pytest.mark.parametrize("path", _SMOKE_ENDPOINTS)
def test_health_endpoint_returns_200(open_client, path):
    resp = open_client.get(path)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    data = resp.json()
    assert data is not None


def test_composite_health_score_is_gone(open_client):
    """The Health Score page was removed 2026-08-26, endpoint included.

    Its one number was a weighted blend of six tiles that each say something
    concrete on their own, and the tiles aggregate paid for it by computing
    all six as dependencies whenever the composite slot was asked for.
    """
    assert open_client.get("/api/health/composite-score").status_code == 404
    body = open_client.get("/api/health/tiles?tiles=composite").json()
    assert "composite" not in body["tiles"]
