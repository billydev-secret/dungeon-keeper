"""Tests for /api/music-playlist/* — the feature's whole admin surface.

Spotify never gets touched: the route module's ``_resolver`` factory is the
one seam (the service and the admin-remove path both build their client
through it), so a stub resolver swapped in there covers connection states,
writes, and write failures alike.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import web_server.routes.music_playlist as mp_routes
from bot_modules.core.db_utils import set_config_value
from bot_modules.music_playlist import music_playlist_store as store
from bot_modules.services.spotify_resolver import SpotifyResolveError
from web_server.auth import DiscordOAuthAuth
from web_server.server import create_app

GID = 123  # FakeCtx default guild
BIG = 1469123456789012345  # a real-size snowflake, > 2**53

ENDPOINTS = [
    ("GET", "/api/music-playlist/status"),
    ("PUT", "/api/music-playlist/settings"),
    ("GET", "/api/music-playlist/window"),
    ("DELETE", "/api/music-playlist/window/1"),
    ("GET", "/api/music-playlist/unmatched"),
    ("POST", "/api/music-playlist/unmatched/1/approve"),
    ("POST", "/api/music-playlist/unmatched/1/reject"),
    ("GET", "/api/music-playlist/history"),
    ("POST", "/api/music-playlist/rescan"),
    ("POST", "/api/music-playlist/reconcile"),
]

_MODIFY = {"playlist-modify-private", "playlist-modify-public"}


class StubResolver:
    """The two playlist writes plus the scope reads, no network."""

    def __init__(self, *, scopes: set[str] | None = None, fail_writes: bool = False):
        self._scopes = set(scopes or _MODIFY)
        self.fail_writes = fail_writes
        self.added: list[tuple[str, list[str]]] = []
        self.removed: list[tuple[str, list[str]]] = []

    async def playlist_scopes(self) -> set[str]:
        return set(self._scopes)

    async def can_modify_playlists(self) -> bool:
        return bool(self._scopes & _MODIFY)

    async def add_tracks_to_playlist(self, playlist_id, track_ids):
        if self.fail_writes:
            raise SpotifyResolveError("Spotify grant is read-only; re-consent needed.")
        self.added.append((playlist_id, list(track_ids)))

    async def remove_tracks_from_playlist(self, playlist_id, track_ids):
        if self.fail_writes:
            raise SpotifyResolveError("Spotify write refused.")
        self.removed.append((playlist_id, list(track_ids)))


@pytest.fixture
def stub_resolver(monkeypatch):
    stub = StubResolver()
    monkeypatch.setattr(mp_routes, "_resolver", lambda ctx: stub)
    return stub


def _seed_track(fake_ctx, *, track_id="t1", message_id=1000, playlist_id="pl1",
                channel_id=555, added_by=42, title="T", artist="A"):
    with fake_ctx.open_db() as conn:
        return store.insert_track(
            conn, GID, playlist_id=playlist_id, track_id=track_id, title=title,
            artist=artist, source_url=None, channel_id=channel_id,
            message_id=message_id, added_by=added_by,
        )


def _seed_unmatched(fake_ctx, *, candidate_track_id="trk1", message_id=9001):
    with fake_ctx.open_db() as conn:
        return store.create_unmatched(
            conn, GID, channel_id=555, message_id=message_id,
            source_url="https://youtu.be/abc123", added_by=42,
            extracted_title="Song Title", candidate_track_id=candidate_track_id,
            candidate_name="Song", candidate_artist="Artist",
            confidence=0.5, reason="confidence_below_threshold",
        )


def _configure(client, **overrides):
    body = {"enabled": True, "playlist_id": "pl1", "channel_id": "555"}
    body.update(overrides)
    r = client.put("/api/music-playlist/settings", json=body)
    assert r.status_code == 200, r.text
    return r.json()["settings"]


# ── authz ─────────────────────────────────────────────────────────────


def test_every_endpoint_rejects_unauthenticated(fake_ctx):
    """Belt to the sweep's braces: no session ⇒ 401/403 on every route."""
    app = create_app(fake_ctx, auth=DiscordOAuthAuth("test-secret", fake_ctx.guild_id))
    client = TestClient(app)
    for method, path in ENDPOINTS:
        r = client.request(method, path, json={} if method in ("PUT", "POST") else None)
        assert r.status_code in (401, 403), f"{method} {path} -> {r.status_code}"


# ── status ────────────────────────────────────────────────────────────


def test_status_defaults(open_client):
    r = open_client.get("/api/music-playlist/status")
    assert r.status_code == 200
    data = r.json()
    assert data["connection"] == "not_connected"
    assert data["settings"] == {
        "enabled": False,
        "channel_id": None,
        "playlist_id": "",
        "window_size": 30,
        "match_threshold": 0.74,
        "remove_on_delete": True,
        "rescan_depth": 200,
    }
    assert data["window_count"] == 0
    assert data["pending_count"] == 0


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        pytest.param("", "not_connected", id="no-grant"),
        pytest.param(
            "playlist-read-private playlist-read-collaborative",
            "read_only",
            id="pre-widening-token",
        ),
        pytest.param(
            "playlist-read-private playlist-modify-private playlist-modify-public",
            "connected",
            id="full-grant",
        ),
    ],
)
def test_status_connection_chip(open_client, fake_ctx, scope, expected):
    if scope:
        with fake_ctx.open_db() as conn:
            set_config_value(conn, "spotify_bot_scope", scope)
    r = open_client.get("/api/music-playlist/status")
    assert r.json()["connection"] == expected


def test_status_counts(open_client, fake_ctx):
    _configure(open_client)
    _seed_track(fake_ctx, track_id="a", message_id=1)
    _seed_track(fake_ctx, track_id="b", message_id=2)
    _seed_unmatched(fake_ctx)
    data = open_client.get("/api/music-playlist/status").json()
    assert data["window_count"] == 2
    assert data["pending_count"] == 1


# ── settings ──────────────────────────────────────────────────────────


def test_settings_roundtrip_and_snowflake_string(open_client):
    settings = _configure(
        open_client,
        channel_id=str(BIG),
        window_size=15,
        match_threshold=0.5,
        remove_on_delete=False,
    )
    assert settings["channel_id"] == str(BIG)
    assert settings["window_size"] == 15
    assert settings["match_threshold"] == 0.5
    assert settings["remove_on_delete"] is False
    # And it reads back identically through status.
    again = open_client.get("/api/music-playlist/status").json()["settings"]
    assert again == settings


def test_settings_partial_update_leaves_the_rest(open_client):
    _configure(open_client, window_size=12)
    r = open_client.put(
        "/api/music-playlist/settings", json={"match_threshold": 0.9}
    )
    settings = r.json()["settings"]
    assert settings["match_threshold"] == 0.9
    assert settings["window_size"] == 12
    assert settings["playlist_id"] == "pl1"
    assert settings["enabled"] is True


def test_settings_clearing_channel_and_playlist(open_client):
    _configure(open_client)
    r = open_client.put(
        "/api/music-playlist/settings", json={"channel_id": "", "playlist_id": ""}
    )
    settings = r.json()["settings"]
    assert settings["channel_id"] is None
    assert settings["playlist_id"] == ""


def test_settings_accepts_playlist_link_and_uri(open_client):
    link = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
    settings = _configure(open_client, playlist_id=link)
    assert settings["playlist_id"] == "37i9dQZF1DXcBWIGoYBM5M"
    settings = _configure(open_client, playlist_id="spotify:playlist:AbC123xyz")
    assert settings["playlist_id"] == "AbC123xyz"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"window_size": 0}, id="window-too-small"),
        pytest.param({"window_size": 201}, id="window-too-big"),
        pytest.param({"match_threshold": -0.1}, id="threshold-negative"),
        pytest.param({"match_threshold": 1.01}, id="threshold-above-one"),
        pytest.param({"channel_id": "general"}, id="channel-not-an-id"),
        pytest.param({"channel_id": "-5"}, id="channel-negative"),
        pytest.param({"playlist_id": "not a playlist!!"}, id="playlist-garbage"),
        pytest.param(
            {"playlist_id": "https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6"},
            id="playlist-is-a-track-link",
        ),
    ],
)
def test_settings_validation_bounds(open_client, body):
    r = open_client.put("/api/music-playlist/settings", json=body)
    assert r.status_code == 422, r.text
    # Nothing was persisted — defaults still stand.
    settings = open_client.get("/api/music-playlist/status").json()["settings"]
    assert settings["window_size"] == 30
    assert settings["match_threshold"] == 0.74


# ── window ────────────────────────────────────────────────────────────


def test_window_empty_without_playlist(open_client):
    data = open_client.get("/api/music-playlist/window").json()
    assert data == {"window_size": 30, "tracks": []}


def test_window_newest_first_with_string_snowflakes(open_client, fake_ctx):
    _configure(open_client)
    _seed_track(fake_ctx, track_id="old", message_id=BIG, channel_id=BIG,
                added_by=BIG)
    _seed_track(fake_ctx, track_id="new", message_id=BIG + 1)
    data = open_client.get("/api/music-playlist/window").json()
    assert [t["track_id"] for t in data["tracks"]] == ["new", "old"]
    oldest = data["tracks"][1]
    assert oldest["message_id"] == str(BIG)
    assert oldest["channel_id"] == str(BIG)
    assert oldest["added_by"] == str(BIG)


def test_admin_remove_marks_row_and_hits_spotify(
    open_client, fake_ctx, stub_resolver
):
    _configure(open_client)
    row_id = _seed_track(fake_ctx, track_id="t1")
    r = open_client.delete(f"/api/music-playlist/window/{row_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["removed_track_id"] == "t1"
    assert body["spotify_removed"] is True
    assert stub_resolver.removed == [("pl1", ["t1"])]
    # Gone from the window, present in history with the admin reason.
    assert open_client.get("/api/music-playlist/window").json()["tracks"] == []
    history = open_client.get("/api/music-playlist/history").json()["history"]
    assert [(h["track_id"], h["removal_reason"]) for h in history] == [
        ("t1", "admin")
    ]
    # Already removed ⇒ 404.
    assert open_client.delete(f"/api/music-playlist/window/{row_id}").status_code == 404


def test_admin_remove_survives_spotify_failure(
    open_client, fake_ctx, stub_resolver
):
    """DB first, Spotify best-effort — the row is removed either way."""
    _configure(open_client)
    row_id = _seed_track(fake_ctx, track_id="t1")
    stub_resolver.fail_writes = True
    body = open_client.delete(f"/api/music-playlist/window/{row_id}").json()
    assert body["ok"] is True
    assert body["spotify_removed"] is False
    assert "refused" in body["error"]
    assert open_client.get("/api/music-playlist/window").json()["tracks"] == []


def test_admin_remove_unknown_row_404(open_client):
    assert open_client.delete("/api/music-playlist/window/999").status_code == 404


# ── unmatched review queue ────────────────────────────────────────────


def test_unmatched_lists_pending_with_string_snowflakes(open_client, fake_ctx):
    item_id = _seed_unmatched(fake_ctx, message_id=BIG)
    data = open_client.get("/api/music-playlist/unmatched").json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["id"] == item_id
    assert item["message_id"] == str(BIG)
    assert item["added_by"] == "42"
    assert item["candidate_track_id"] == "trk1"
    assert item["confidence"] == 0.5
    assert item["status"] == "pending"


def test_approve_adds_track_and_resolves_once(
    open_client, fake_ctx, stub_resolver
):
    _configure(open_client)
    item_id = _seed_unmatched(fake_ctx)
    r = open_client.post(f"/api/music-playlist/unmatched/{item_id}/approve")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added_track_id"] == "trk1"
    assert body["was_duplicate"] is False
    assert stub_resolver.added == [("pl1", ["trk1"])]
    # It landed in the window and left the queue.
    tracks = open_client.get("/api/music-playlist/window").json()["tracks"]
    assert [t["track_id"] for t in tracks] == ["trk1"]
    assert open_client.get("/api/music-playlist/unmatched").json()["items"] == []
    # The status flip is exactly-once: a second approve is a conflict.
    again = open_client.post(f"/api/music-playlist/unmatched/{item_id}/approve")
    assert again.status_code == 409


def test_approve_duplicate_candidate_writes_nothing(
    open_client, fake_ctx, stub_resolver
):
    _configure(open_client)
    _seed_track(fake_ctx, track_id="trk1")
    item_id = _seed_unmatched(fake_ctx, candidate_track_id="trk1")
    body = open_client.post(
        f"/api/music-playlist/unmatched/{item_id}/approve"
    ).json()
    assert body["was_duplicate"] is True
    assert stub_resolver.added == []


def test_approve_unknown_item_404(open_client, stub_resolver, fake_ctx):
    _configure(open_client)
    r = open_client.post("/api/music-playlist/unmatched/999/approve")
    assert r.status_code == 404


def test_approve_without_playlist_409(open_client, fake_ctx, stub_resolver):
    item_id = _seed_unmatched(fake_ctx)
    r = open_client.post(f"/api/music-playlist/unmatched/{item_id}/approve")
    assert r.status_code == 409
    assert "playlist" in r.json()["detail"].lower()


def test_approve_failed_write_reopens_item(open_client, fake_ctx, stub_resolver):
    _configure(open_client)
    item_id = _seed_unmatched(fake_ctx)
    stub_resolver.fail_writes = True
    r = open_client.post(f"/api/music-playlist/unmatched/{item_id}/approve")
    assert r.status_code == 409
    assert "read-only" in r.json()["detail"]
    # The item is pending again — retryable after re-consent.
    items = open_client.get("/api/music-playlist/unmatched").json()["items"]
    assert [i["id"] for i in items] == [item_id]
    stub_resolver.fail_writes = False
    assert (
        open_client.post(
            f"/api/music-playlist/unmatched/{item_id}/approve"
        ).status_code
        == 200
    )


def test_reject_resolves_once(open_client, fake_ctx, stub_resolver):
    item_id = _seed_unmatched(fake_ctx)
    assert (
        open_client.post(
            f"/api/music-playlist/unmatched/{item_id}/reject"
        ).status_code
        == 200
    )
    assert open_client.get("/api/music-playlist/unmatched").json()["items"] == []
    assert (
        open_client.post(
            f"/api/music-playlist/unmatched/{item_id}/reject"
        ).status_code
        == 404
    )


# ── history ───────────────────────────────────────────────────────────


def test_history_shows_reasons_and_hides_duplicate_references(
    open_client, fake_ctx
):
    _configure(open_client, window_size=1)
    _seed_track(fake_ctx, track_id="a", message_id=1)
    with fake_ctx.open_db() as conn:
        # A duplicate reference row — bookkeeping, never history.
        store.record_duplicate_reference(
            conn, GID, playlist_id="pl1", track_id="a", title="T", artist="A",
            source_url=None, channel_id=555, message_id=2, added_by=43,
        )
        store.insert_track(
            conn, GID, playlist_id="pl1", track_id="b", title="T", artist="A",
            source_url=None, channel_id=555, message_id=3, added_by=42,
        )
        store.trim_to_window(conn, GID, "pl1", 1)
    data = open_client.get("/api/music-playlist/history").json()
    assert [(h["track_id"], h["removal_reason"]) for h in data["history"]] == [
        ("a", "rolled_off")
    ]


def test_history_limit_clamped(open_client, fake_ctx):
    _configure(open_client, window_size=1)
    for i in range(4):
        _seed_track(fake_ctx, track_id=f"t{i}", message_id=100 + i)
        with fake_ctx.open_db() as conn:
            store.trim_to_window(conn, GID, "pl1", 1)
    data = open_client.get("/api/music-playlist/history?limit=2").json()
    assert len(data["history"]) == 2
    # A hostile limit can't dump unbounded rows (SQLite reads -1 as no limit).
    data = open_client.get("/api/music-playlist/history?limit=-1").json()
    assert len(data["history"]) == 1


# ── maintenance ───────────────────────────────────────────────────────


@pytest.mark.parametrize("action", ["rescan", "reconcile"])
def test_maintenance_503_without_bot(open_client, action):
    r = open_client.post(f"/api/music-playlist/{action}")
    assert r.status_code == 503


def test_maintenance_delegates_to_cog(open_client, fake_ctx):
    calls: list[tuple[str, int]] = []

    class Cog:
        async def rescan_channel(self, guild_id: int) -> dict:
            calls.append(("rescan", guild_id))
            return {"ok": True, "scanned": 5}

        async def reconcile_playlist(
            self, guild_id: int, *, confirm_removals: bool = False
        ) -> dict:
            calls.append(("reconcile", guild_id))
            return {"ok": True, "drift": 0, "confirmed": confirm_removals}

    cog = Cog()
    fake_ctx.bot = SimpleNamespace(
        get_cog=lambda name: cog if name == "MusicPlaylistCog" else None
    )
    r = open_client.post("/api/music-playlist/rescan")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "scanned": 5}
    # Unconfirmed by default: the service withholds a bulk delete until the
    # panel confirms, so the flag has to survive the route hop.
    r = open_client.post("/api/music-playlist/reconcile")
    assert r.json() == {"ok": True, "drift": 0, "confirmed": False}
    r = open_client.post(
        "/api/music-playlist/reconcile?confirm_removals=true"
    )
    assert r.json()["confirmed"] is True
    assert calls == [("rescan", GID), ("reconcile", GID), ("reconcile", GID)]
