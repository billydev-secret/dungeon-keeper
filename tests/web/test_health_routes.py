"""Smoke tests for /api/health/* endpoints — 200 + valid JSON shape."""

from __future__ import annotations

import pytest


_SMOKE_ENDPOINTS = [
    "/api/health/tiles",
    "/api/health/dau-mau",
    "/api/health/composite-score",
    "/api/health/newcomer-funnel",
    "/api/health/churn-risk",
    "/api/health/mod-workload",
]


@pytest.mark.parametrize("path", _SMOKE_ENDPOINTS)
def test_health_endpoint_returns_200(open_client, path):
    resp = open_client.get(path)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    data = resp.json()
    assert data is not None
