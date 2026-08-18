"""Tests for SpotifyResolver playlist writes + scope checking.

Covers the write half of ``bot_modules/services/spotify_resolver.py``:
scope parsing from the ``spotify_bot_scope`` config key, the read-only
guard on both writers, 100-URI chunking, and the distinct wording of a
real Spotify 403 versus a missing modify scope.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from spotipy.exceptions import SpotifyException

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.services.spotify_resolver import (
    SpotifyResolveError,
    SpotifyResolver,
)

READ_SCOPES = "playlist-read-private playlist-read-collaborative"
FULL_SCOPES = READ_SCOPES + " playlist-modify-private playlist-modify-public"


def _set_scope(db_path: Path, scope: str) -> None:
    with open_db(db_path) as conn:
        set_config_value(conn, "spotify_bot_scope", scope)


def _resolver(db_path: Path | None) -> SpotifyResolver:
    return SpotifyResolver(client_id="cid", client_secret="csec", db_path=db_path)


def _writable_resolver(fake_client) -> SpotifyResolver:
    """Resolver with scope + user client stubbed past the DB/OAuth machinery."""
    resolver = _resolver(None)

    async def _scopes() -> set[str]:
        return set(FULL_SCOPES.split())

    async def _client():
        return fake_client

    resolver.playlist_scopes = _scopes  # type: ignore[method-assign]
    resolver._get_user_client = _client  # type: ignore[method-assign]
    return resolver


# ── playlist_scopes / can_modify_playlists ───────────────────────────


async def test_playlist_scopes_reads_config(sync_db_path):
    _set_scope(sync_db_path, FULL_SCOPES)
    assert await _resolver(sync_db_path).playlist_scopes() == set(FULL_SCOPES.split())


async def test_playlist_scopes_empty_when_unset(sync_db_path):
    assert await _resolver(sync_db_path).playlist_scopes() == set()


async def test_playlist_scopes_empty_without_db():
    assert await _resolver(None).playlist_scopes() == set()


@pytest.mark.parametrize(
    "scope, expected",
    [
        pytest.param(FULL_SCOPES, True, id="both-modify"),
        pytest.param(
            READ_SCOPES + " playlist-modify-private", True, id="modify-private"
        ),
        pytest.param(
            READ_SCOPES + " playlist-modify-public", True, id="modify-public"
        ),
        pytest.param(READ_SCOPES, False, id="read-only"),
        pytest.param("", False, id="unset"),
    ],
)
async def test_can_modify_playlists(sync_db_path, scope, expected):
    if scope:
        _set_scope(sync_db_path, scope)
    assert await _resolver(sync_db_path).can_modify_playlists() is expected


# ── missing-scope guard ──────────────────────────────────────────────


@pytest.mark.parametrize("method", ["add_tracks_to_playlist", "remove_tracks_from_playlist"])
async def test_write_without_modify_scope_raises_reconsent_error(
    sync_db_path, method
):
    _set_scope(sync_db_path, READ_SCOPES)
    resolver = _resolver(sync_db_path)
    with pytest.raises(SpotifyResolveError) as exc_info:
        await getattr(resolver, method)("pl123", ["t1"])
    msg = str(exc_info.value)
    assert "read-only" in msg
    assert "re-authorize at /spotify/authorize" in msg
    assert "playlist-modify" in msg


async def test_write_with_scope_but_no_user_client_raises(sync_db_path):
    # Scope granted but no refresh token stored — the client is unavailable.
    _set_scope(sync_db_path, FULL_SCOPES)
    resolver = _resolver(sync_db_path)
    with pytest.raises(SpotifyResolveError, match="re-authorize at /spotify/authorize"):
        await resolver.add_tracks_to_playlist("pl123", ["t1"])


# ── add_tracks_to_playlist ───────────────────────────────────────────


async def test_add_empty_list_is_noop():
    client = MagicMock()
    resolver = _writable_resolver(client)
    assert await resolver.add_tracks_to_playlist("pl123", []) == 0
    client.playlist_add_items.assert_not_called()


@pytest.mark.parametrize(
    "count, chunk_sizes",
    [
        pytest.param(1, [1], id="single"),
        pytest.param(100, [100], id="exactly-100"),
        pytest.param(101, [100, 1], id="just-over-100"),
        pytest.param(250, [100, 100, 50], id="multi-chunk"),
    ],
)
async def test_add_chunks_at_100(count, chunk_sizes):
    client = MagicMock()
    resolver = _writable_resolver(client)
    track_ids = [f"t{i}" for i in range(count)]

    added = await resolver.add_tracks_to_playlist("pl123", track_ids)

    assert added == count
    calls = client.playlist_add_items.call_args_list
    assert [len(c.args[1]) for c in calls] == chunk_sizes
    assert all(c.args[0] == "pl123" for c in calls)
    sent = [uri for c in calls for uri in c.args[1]]
    assert sent == [f"spotify:track:{tid}" for tid in track_ids]


# ── remove_tracks_from_playlist ──────────────────────────────────────


async def test_remove_empty_list_is_noop():
    client = MagicMock()
    resolver = _writable_resolver(client)
    assert await resolver.remove_tracks_from_playlist("pl123", []) == 0
    client.playlist_remove_all_occurrences_of_items.assert_not_called()


async def test_remove_chunks_at_100():
    client = MagicMock()
    resolver = _writable_resolver(client)
    track_ids = [f"t{i}" for i in range(150)]

    removed = await resolver.remove_tracks_from_playlist("pl123", track_ids)

    assert removed == 150
    calls = client.playlist_remove_all_occurrences_of_items.call_args_list
    assert [len(c.args[1]) for c in calls] == [100, 50]
    sent = [uri for c in calls for uri in c.args[1]]
    assert sent == [f"spotify:track:{tid}" for tid in track_ids]


# ── Spotify-side failures stay distinct from missing scope ───────────


@pytest.mark.parametrize(
    "method, client_attr",
    [
        pytest.param(
            "add_tracks_to_playlist", "playlist_add_items", id="add"
        ),
        pytest.param(
            "remove_tracks_from_playlist",
            "playlist_remove_all_occurrences_of_items",
            id="remove",
        ),
    ],
)
async def test_spotify_403_surfaces_distinctly(method, client_attr):
    client = MagicMock()
    getattr(client, client_attr).side_effect = SpotifyException(
        403, -1, "Insufficient client scope"
    )
    resolver = _writable_resolver(client)

    with pytest.raises(SpotifyResolveError) as exc_info:
        await getattr(resolver, method)("pl123", ["t1"])
    msg = str(exc_info.value)
    # Scope was granted, so this is a playlist-access denial — worded as
    # such, never as the re-consent instruction.
    assert "403" in msg
    assert "re-authorize" not in msg


async def test_spotify_404_names_the_playlist_id_problem():
    client = MagicMock()
    client.playlist_add_items.side_effect = SpotifyException(404, -1, "Not found")
    resolver = _writable_resolver(client)

    with pytest.raises(SpotifyResolveError, match="Playlist not found"):
        await resolver.add_tracks_to_playlist("pl123", ["t1"])


async def test_spotify_other_errors_pass_through():
    client = MagicMock()
    client.playlist_add_items.side_effect = SpotifyException(500, -1, "Server error")
    resolver = _writable_resolver(client)

    with pytest.raises(SpotifyResolveError, match="Spotify API error: 500"):
        await resolver.add_tracks_to_playlist("pl123", ["t1"])


# ── playlist_track_ids (the reconcile action's read half) ────────────


def _item(track_id, *, is_local=False):
    return {"track": {"id": track_id, "is_local": is_local}}


async def test_playlist_track_ids_pages_and_skips_locals():
    client = MagicMock()
    client.playlist_items.side_effect = [
        {"items": [_item("t1"), _item(None), _item("t2", is_local=True)],
         "next": "page2"},
        {"items": [_item("t3"), {"track": None}], "next": None},
    ]
    resolver = _writable_resolver(client)

    ids = await resolver.playlist_track_ids("pl123")

    # Local files and episode-shaped items (no track / no id) are skipped;
    # order is playlist order across pages.
    assert ids == ["t1", "t3"]
    offsets = [c.kwargs["offset"] for c in client.playlist_items.call_args_list]
    assert offsets == [0, 100]


async def test_playlist_track_ids_rejects_an_all_null_read():
    """Items reported but every track null = unusable read, not an empty list.

    The prod incident: Development-mode apps get ``track: null`` on every
    playlist item, ``playlist_track_ids`` returned [], and reconcile re-added
    its whole window as duplicates on each click. An unusable read must abort.
    """
    client = MagicMock()
    client.playlist_items.side_effect = [
        {"items": [{"track": None}] * 3, "next": "page2"},
        {"items": [{"track": None}] * 2, "next": None},
    ]
    resolver = _writable_resolver(client)

    with pytest.raises(SpotifyResolveError, match="no track data"):
        await resolver.playlist_track_ids("pl123")


async def test_playlist_track_ids_empty_playlist_is_empty():
    client = MagicMock()
    client.playlist_items.return_value = {"items": [], "next": None}
    resolver = _writable_resolver(client)
    assert await resolver.playlist_track_ids("pl123") == []


async def test_playlist_track_ids_all_local_files_is_not_an_error():
    # Local files carry a real track object (id null, is_local true) — an
    # all-local playlist is a readable playlist with nothing writable, not
    # the null-track failure shape.
    client = MagicMock()
    client.playlist_items.return_value = {
        "items": [_item(None, is_local=True), _item(None, is_local=True)],
        "next": None,
    }
    resolver = _writable_resolver(client)
    assert await resolver.playlist_track_ids("pl123") == []


async def test_playlist_track_ids_falls_back_without_user_client():
    fallback = MagicMock()
    fallback.playlist_items.return_value = {"items": [_item("t1")], "next": None}
    resolver = _resolver(None)

    async def _no_user_client():
        return None

    resolver._get_user_client = _no_user_client  # type: ignore[method-assign]
    resolver._client = fallback  # skip credential bootstrap

    assert await resolver.playlist_track_ids("pl123") == ["t1"]
