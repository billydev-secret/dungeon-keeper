"""Smoke tests for /api/health/* endpoints — 200 + valid JSON shape."""

from __future__ import annotations

import pytest


_SMOKE_ENDPOINTS = [
    "/api/health/tiles",
    "/api/health/dau-mau",
    "/api/health/newcomer-funnel",
    "/api/health/gini",
    "/api/health/mod-workload",
    "/api/health/mod-coverage",
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


def test_mod_coverage_shape(open_client):
    """The panel draws straight off these keys, so their presence is the API.

    Twenty-four hour rows always, even on an empty guild: the chart's x-axis is
    the clock, and a short array would silently misalign every bar with the
    hour it claims to describe.
    """
    data = open_client.get("/api/health/mod-coverage").json()

    assert len(data["labels"]) == 24
    assert len(data["hours"]) == 24
    assert [r["hour"] for r in data["hours"]] == list(range(24))
    assert len(data["server_current"]) == 24
    assert len(data["mod_current"]) == 24
    for key in ("busiest_uncovered", "longest_gap"):
        assert key in data
    assert data["mod_count"] >= 0


def test_mod_coverage_counts_a_wider_circle_than_mod_workload(live_guild, open_client):
    """A member who can only delete messages is present, but takes no actions.

    Mod Coverage asks who is *around*; Mod Workload asks who *acts*. Wiring the
    coverage route to ``mod_ids`` instead of ``msg_mod_ids`` would silently
    shrink the population to the kick/ban holders and make the report answer
    the question the panel next to it already answers.
    """
    from tests.web.conftest import StubMember

    live_guild([
        StubMember(1, mod=True),                          # kick/ban + delete
        StubMember(2, mod=False, manage_messages=True),   # delete only
        StubMember(3, mod=False),                         # ordinary member
    ])

    assert open_client.get("/api/health/mod-coverage").json()["mod_count"] == 2
