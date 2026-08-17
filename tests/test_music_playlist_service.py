"""Tests for the music playlist pipeline service.

Covers ``bot_modules/music_playlist/music_playlist_service.py`` with the
Spotify client and the oEmbed fetch mocked — no network. The heart of the
coverage, per the spec: happy path adds and trims, below-threshold goes to
the review queue and adds nothing, duplicates within the live window, a
rolled-off track re-adds, album links skip by default and expand on the
dial, deletes respect the other-live-referrer rule, and every Spotify write
failure (missing modify scope, 403, exhausted 429) surfaces as a state
instead of a crash.
"""

from __future__ import annotations

import httpx
import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.music_playlist import music_playlist_store as store
from bot_modules.music_playlist.music_playlist_logic import (
    SpotifyTrack,
    YouTubeMetadata,
    parse_spotify_url,
)
from bot_modules.music_playlist.music_playlist_service import (
    REASON_LOW_CONFIDENCE,
    REASON_NO_CANDIDATES,
    REASON_NO_METADATA,
    DEFAULT_MUSIC_PLAYLIST_SETTINGS,
    MusicPlaylistService,
    fetch_youtube_metadata,
    load_music_playlist_settings,
    save_music_playlist_settings,
    search_spotify_tracks,
)
from bot_modules.services.spotify_resolver import (
    SpotifyResolveError,
    SpotifyResolveResult,
)
from bot_modules.services.spotify_resolver import (
    SpotifyTrack as ResolverTrack,
)

GUILD = 1400000000000000001
CHANNEL = 1400000000000000002
PLAYLIST = "5RollingWindow"
ALICE = 1400000000000000101
BOB = 1400000000000000102
MOD = 1400000000000000103

READ_SCOPES = {"playlist-read-private", "playlist-read-collaborative"}
FULL_SCOPES = READ_SCOPES | {"playlist-modify-private", "playlist-modify-public"}

VIDEO_ID = "abcdefghijk"
YT_URL = f"https://youtu.be/{VIDEO_ID}"
GOOD_META = YouTubeMetadata(
    video_id=VIDEO_ID,
    title="Rick Astley - Never Gonna Give You Up (Official Video)",
    channel_name="Rick Astley",
    source_url=f"https://www.youtube.com/watch?v={VIDEO_ID}",
)
GOOD_CANDIDATE = SpotifyTrack(
    track_id="tRick", name="Never Gonna Give You Up", artists=["Rick Astley"]
)
BAD_CANDIDATE = SpotifyTrack(
    track_id="tWrong", name="Totally Different Song", artists=["Nobody Known"]
)


def track_url(track_id: str) -> str:
    return f"https://open.spotify.com/track/{track_id}"


class FakeSpotify:
    """Duck-typed SpotifyResolver mirroring its playlist-write contract."""

    def __init__(self, *, scopes: set[str] | None = None):
        self.scopes = set(FULL_SCOPES if scopes is None else scopes)
        # track_id -> (title, [artists]) for direct-link resolution.
        self.catalog: dict[str, tuple[str, list[str]]] = {}
        # album/playlist item_id -> [(track_id, title, [artists])].
        self.collections: dict[str, list[tuple[str, str, list[str]]]] = {}
        # playlist_id -> track ids currently on the actual playlist.
        self.playlists: dict[str, list[str]] = {}
        self.add_error: Exception | None = None
        self.remove_error: Exception | None = None
        self.read_error: Exception | None = None
        self.resolve_error: Exception | None = None
        self.add_calls: list[tuple[str, list[str]]] = []
        self.remove_calls: list[tuple[str, list[str]]] = []

    async def playlist_scopes(self) -> set[str]:
        return set(self.scopes)

    async def can_modify_playlists(self) -> bool:
        return bool(
            self.scopes & {"playlist-modify-public", "playlist-modify-private"}
        )

    def _resolver_track(
        self, track_id: str, title: str, artists: list[str]
    ) -> ResolverTrack:
        return ResolverTrack(
            title=title,
            artists=artists,
            duration_ms=0,
            isrc=None,
            spotify_url=track_url(track_id),
        )

    async def resolve(self, url: str) -> SpotifyResolveResult:
        if self.resolve_error is not None:
            raise self.resolve_error
        parsed = parse_spotify_url(url)
        assert parsed is not None
        if parsed.link_type.value == "spotify_track":
            if parsed.item_id not in self.catalog:
                raise SpotifyResolveError("Spotify API error: 404 not found")
            title, artists = self.catalog[parsed.item_id]
            return SpotifyResolveResult(
                kind="track",
                name=None,
                tracks=[self._resolver_track(parsed.item_id, title, artists)],
            )
        entries = self.collections.get(parsed.item_id, [])
        return SpotifyResolveResult(
            kind="album",
            name="Some Album",
            tracks=[self._resolver_track(t, n, a) for t, n, a in entries],
        )

    def _check_write(self) -> None:
        # Mirrors _get_write_client: scope gate first, worded in re-consent
        # terms; then whatever failure the test configured.
        if not (
            self.scopes & {"playlist-modify-public", "playlist-modify-private"}
        ):
            raise SpotifyResolveError(
                "Spotify authorization is read-only — re-authorize at "
                "/spotify/authorize to grant playlist-modify."
            )

    async def playlist_track_ids(self, playlist_id: str) -> list[str]:
        if self.read_error is not None:
            raise self.read_error
        return list(self.playlists.get(playlist_id, []))

    async def add_tracks_to_playlist(
        self, playlist_id: str, track_ids: list[str]
    ) -> int:
        if not track_ids:
            return 0
        self._check_write()
        if self.add_error is not None:
            raise self.add_error
        self.playlists.setdefault(playlist_id, []).extend(track_ids)
        self.add_calls.append((playlist_id, list(track_ids)))
        return len(track_ids)

    async def remove_tracks_from_playlist(
        self, playlist_id: str, track_ids: list[str]
    ) -> int:
        if not track_ids:
            return 0
        self._check_write()
        if self.remove_error is not None:
            raise self.remove_error
        current = self.playlists.setdefault(playlist_id, [])
        self.playlists[playlist_id] = [t for t in current if t not in track_ids]
        self.remove_calls.append((playlist_id, list(track_ids)))
        return len(track_ids)


def seed_settings(db_path, **overrides) -> None:
    values: dict[str, object] = {
        "enabled": True,
        "channel_id": CHANNEL,
        "playlist_id": PLAYLIST,
    }
    values.update(overrides)
    with open_db(db_path) as conn:
        save_music_playlist_settings(conn, GUILD, values)


def make_service(db_path, spotify, *, meta=None, candidates=None):
    """Service with the network edges stubbed.

    ``meta``: what the oEmbed fetch returns (None = metadata unavailable);
    ``candidates``: what every Spotify search returns.
    """

    async def fetch(video_id: str) -> YouTubeMetadata | None:
        return meta

    async def search(query: str, limit: int) -> list[SpotifyTrack]:
        return list(candidates or [])

    return MusicPlaylistService(
        db_path, spotify, youtube_fetcher=fetch, search_tracks=search
    )


def window_ids(db_path) -> list[str]:
    with open_db(db_path) as conn:
        return [r["track_id"] for r in store.live_window(conn, GUILD, PLAYLIST)]


def pending_rows(db_path):
    with open_db(db_path) as conn:
        return [dict(r) for r in store.list_pending(conn, GUILD)]


# ── Settings ──────────────────────────────────────────────────────────


def test_settings_defaults(sync_db_path):
    with open_db(sync_db_path) as conn:
        settings = load_music_playlist_settings(conn, GUILD)
    assert settings == DEFAULT_MUSIC_PLAYLIST_SETTINGS
    assert settings.enabled is False
    assert settings.window_size == 30
    assert settings.match_threshold == 0.74
    assert settings.expand_albums is False
    assert settings.remove_on_delete is True


def test_settings_roundtrip_and_unknown_key(sync_db_path):
    with open_db(sync_db_path) as conn:
        save_music_playlist_settings(conn, GUILD, {
            "enabled": True,
            "channel_id": CHANNEL,
            "playlist_id": PLAYLIST,
            "match_threshold": 0.81,
            "expand_albums": True,
        })
        settings = load_music_playlist_settings(conn, GUILD)
        assert settings.enabled is True
        assert settings.channel_id == CHANNEL
        assert settings.playlist_id == PLAYLIST
        assert settings.match_threshold == 0.81
        assert settings.expand_albums is True
        # Untouched keys keep their defaults.
        assert settings.window_size == 30
        with pytest.raises(KeyError):
            save_music_playlist_settings(conn, GUILD, {"widow_size": 5})


def test_settings_unparseable_values_fall_back(sync_db_path):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, "music_playlist_window_size", "banana", GUILD)
        set_config_value(conn, "music_playlist_match_threshold", "high", GUILD)
        settings = load_music_playlist_settings(conn, GUILD)
    assert settings.window_size == 30
    assert settings.match_threshold == 0.74


# ── Happy path: direct links, window, trim ────────────────────────────


async def test_direct_track_link_adds(sync_db_path):
    seed_settings(sync_db_path)
    spotify = FakeSpotify()
    spotify.catalog["t1"] = ("Song One", ["Artist A", "Artist B"])
    svc = make_service(sync_db_path, spotify)
    summary = await svc.process_message(GUILD, CHANNEL, 101, track_url("t1"), ALICE)
    assert summary.added_track_ids == ["t1"]
    assert summary.links_found == 1
    assert not summary.errors and not summary.write_blocked
    assert spotify.playlists[PLAYLIST] == ["t1"]
    with open_db(sync_db_path) as conn:
        rows = store.live_window(conn, GUILD, PLAYLIST)
        assert [r["track_id"] for r in rows] == ["t1"]
        assert rows[0]["title"] == "Song One"
        assert rows[0]["artist"] == "Artist A, Artist B"
        assert rows[0]["added_by"] == ALICE
        assert store.is_message_processed(conn, GUILD, 101)


async def test_window_trims_and_removes_from_spotify(sync_db_path):
    seed_settings(sync_db_path, window_size=2)
    spotify = FakeSpotify()
    for tid in ("t1", "t2", "t3"):
        spotify.catalog[tid] = (f"Song {tid}", ["A"])
    svc = make_service(sync_db_path, spotify)
    await svc.process_message(GUILD, CHANNEL, 101, track_url("t1"), ALICE)
    await svc.process_message(GUILD, CHANNEL, 102, track_url("t2"), ALICE)
    summary = await svc.process_message(GUILD, CHANNEL, 103, track_url("t3"), BOB)
    assert summary.rolled_off_track_ids == ["t1"]
    assert window_ids(sync_db_path) == ["t3", "t2"]
    assert spotify.remove_calls == [(PLAYLIST, ["t1"])]
    assert spotify.playlists[PLAYLIST] == ["t2", "t3"]


async def test_duplicate_in_live_window(sync_db_path):
    seed_settings(sync_db_path)
    spotify = FakeSpotify()
    spotify.catalog["t1"] = ("Song", ["A"])
    svc = make_service(sync_db_path, spotify)
    await svc.process_message(GUILD, CHANNEL, 101, track_url("t1"), ALICE)
    summary = await svc.process_message(GUILD, CHANNEL, 102, track_url("t1"), BOB)
    assert summary.added_track_ids == []
    assert summary.duplicate_count == 1
    assert len(spotify.add_calls) == 1  # second post never hit Spotify
    assert window_ids(sync_db_path) == ["t1"]


async def test_rolled_off_track_can_be_readded(sync_db_path):
    seed_settings(sync_db_path, window_size=1)
    spotify = FakeSpotify()
    spotify.catalog["t1"] = ("Song 1", ["A"])
    spotify.catalog["t2"] = ("Song 2", ["B"])
    svc = make_service(sync_db_path, spotify)
    await svc.process_message(GUILD, CHANNEL, 101, track_url("t1"), ALICE)
    await svc.process_message(GUILD, CHANNEL, 102, track_url("t2"), ALICE)
    assert window_ids(sync_db_path) == ["t2"]
    # t1 rolled off months ago (well, one message ago) — postable again.
    summary = await svc.process_message(GUILD, CHANNEL, 103, track_url("t1"), BOB)
    assert summary.added_track_ids == ["t1"]
    assert window_ids(sync_db_path) == ["t1"]


async def test_message_ledger_skips_reprocessing(sync_db_path):
    seed_settings(sync_db_path)
    spotify = FakeSpotify()
    spotify.catalog["t1"] = ("Song", ["A"])
    svc = make_service(sync_db_path, spotify)
    await svc.process_message(GUILD, CHANNEL, 101, track_url("t1"), ALICE)
    summary = await svc.process_message(GUILD, CHANNEL, 101, track_url("t1"), ALICE)
    assert summary.skipped is True
    assert len(spotify.add_calls) == 1


async def test_no_links_still_ledgered(sync_db_path):
    seed_settings(sync_db_path)
    svc = make_service(sync_db_path, FakeSpotify())
    summary = await svc.process_message(GUILD, CHANNEL, 101, "just chatting", ALICE)
    assert summary.links_found == 0
    assert summary.skipped is False
    with open_db(sync_db_path) as conn:
        assert store.is_message_processed(conn, GUILD, 101)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"enabled": False}, id="disabled"),
        pytest.param({"playlist_id": ""}, id="no-playlist"),
        pytest.param({"channel_id": CHANNEL + 5}, id="wrong-channel"),
    ],
)
async def test_gates_skip_without_touching_anything(sync_db_path, overrides):
    seed_settings(sync_db_path, **overrides)
    spotify = FakeSpotify()
    spotify.catalog["t1"] = ("Song", ["A"])
    svc = make_service(sync_db_path, spotify)
    summary = await svc.process_message(GUILD, CHANNEL, 101, track_url("t1"), ALICE)
    assert summary.skipped is True
    assert not spotify.add_calls
    with open_db(sync_db_path) as conn:
        assert not store.is_message_processed(conn, GUILD, 101)


async def test_unresolvable_link_does_not_sink_the_message(sync_db_path):
    # First link 404s at Spotify; the second still lands.
    seed_settings(sync_db_path)
    spotify = FakeSpotify()
    spotify.catalog["t2"] = ("Song 2", ["A"])
    svc = make_service(sync_db_path, spotify)
    summary = await svc.process_message(
        GUILD, CHANNEL, 101, f"{track_url('tGone')} {track_url('t2')}", ALICE
    )
    assert summary.added_track_ids == ["t2"]
    assert any("404" in e for e in summary.errors)
    assert summary.write_blocked is False


# ── Album / playlist links ────────────────────────────────────────────


async def test_album_link_skipped_by_default(sync_db_path):
    seed_settings(sync_db_path)
    spotify = FakeSpotify()
    spotify.collections["alb1"] = [
        ("t1", "Song 1", ["A"]), ("t2", "Song 2", ["A"]),
    ]
    svc = make_service(sync_db_path, spotify)
    summary = await svc.process_message(
        GUILD, CHANNEL, 101, "https://open.spotify.com/album/alb1", ALICE
    )
    assert summary.skipped_collections == 1
    assert summary.added_track_ids == []
    assert not spotify.add_calls
    assert pending_rows(sync_db_path) == []  # skipped, not queued


async def test_album_link_expands_when_dial_on(sync_db_path):
    seed_settings(sync_db_path, expand_albums=True)
    spotify = FakeSpotify()
    spotify.collections["alb1"] = [
        ("t1", "Song 1", ["A"]), ("t2", "Song 2", ["A"]),
    ]
    svc = make_service(sync_db_path, spotify)
    summary = await svc.process_message(
        GUILD, CHANNEL, 101, "https://open.spotify.com/album/alb1", ALICE
    )
    assert summary.added_track_ids == ["t1", "t2"]
    assert window_ids(sync_db_path) == ["t2", "t1"]


# ── YouTube resolution ────────────────────────────────────────────────


async def test_confident_youtube_match_adds(sync_db_path):
    seed_settings(sync_db_path)
    spotify = FakeSpotify()
    svc = make_service(
        sync_db_path, spotify, meta=GOOD_META,
        candidates=[BAD_CANDIDATE, GOOD_CANDIDATE],
    )
    summary = await svc.process_message(GUILD, CHANNEL, 101, YT_URL, ALICE)
    assert summary.added_track_ids == ["tRick"]
    assert summary.unmatched_ids == []
    with open_db(sync_db_path) as conn:
        row = store.live_window(conn, GUILD, PLAYLIST)[0]
        assert row["title"] == "Never Gonna Give You Up"
        assert row["source_url"] == YT_URL


async def test_below_threshold_queues_and_adds_nothing(sync_db_path):
    seed_settings(sync_db_path)
    spotify = FakeSpotify()
    svc = make_service(
        sync_db_path, spotify, meta=GOOD_META, candidates=[BAD_CANDIDATE]
    )
    summary = await svc.process_message(GUILD, CHANNEL, 101, YT_URL, ALICE)
    assert summary.added_track_ids == []
    assert not spotify.add_calls
    assert len(summary.unmatched_ids) == 1
    (row,) = pending_rows(sync_db_path)
    assert row["reason"] == REASON_LOW_CONFIDENCE
    assert row["candidate_track_id"] == "tWrong"
    assert row["confidence"] < 0.74
    assert row["extracted_title"] == GOOD_META.title
    assert row["added_by"] == ALICE


@pytest.mark.parametrize(
    ("meta", "candidates", "reason"),
    [
        pytest.param(None, [GOOD_CANDIDATE], REASON_NO_METADATA,
                     id="metadata-unavailable"),
        pytest.param(GOOD_META, [], REASON_NO_CANDIDATES,
                     id="no-candidates"),
    ],
)
async def test_unresolvable_youtube_link_queues(
    sync_db_path, meta, candidates, reason
):
    seed_settings(sync_db_path)
    svc = make_service(sync_db_path, FakeSpotify(), meta=meta, candidates=candidates)
    summary = await svc.process_message(GUILD, CHANNEL, 101, YT_URL, ALICE)
    assert summary.added_track_ids == []
    assert len(summary.unmatched_ids) == 1
    (row,) = pending_rows(sync_db_path)
    assert row["reason"] == reason
    assert row["candidate_track_id"] is None


# ── Spotify write failures surface as state, never a crash ────────────


async def test_missing_modify_scope_reports_itself(sync_db_path):
    seed_settings(sync_db_path)
    spotify = FakeSpotify(scopes=READ_SCOPES)
    spotify.catalog["t1"] = ("Song", ["A"])
    svc = make_service(sync_db_path, spotify)
    summary = await svc.process_message(GUILD, CHANNEL, 101, track_url("t1"), ALICE)
    assert summary.write_blocked is True
    assert any("read-only" in e and "re-authorize" in e for e in summary.errors)
    assert summary.added_track_ids == []
    assert window_ids(sync_db_path) == []  # nothing recorded as live
    with open_db(sync_db_path) as conn:
        # Seen, but NOT terminally processed. No track row was inserted, so
        # reconcile cannot recover this song — only a rescan can, and a rescan
        # skips anything the ledger calls processed. Recording the read-only
        # window as terminal silently and permanently dropped every song
        # posted before the owner re-consented.
        assert not store.is_message_processed(conn, GUILD, 101)
        row = conn.execute(
            "SELECT status FROM music_playlist_messages "
            "WHERE guild_id = ? AND message_id = ?",
            (GUILD, 101),
        ).fetchone()
        assert row["status"] == store.STATUS_WRITE_FAILED


async def test_write_blocked_message_re_fires_after_reconsent(sync_db_path):
    """The read-only window is recoverable: re-process lands the track."""
    seed_settings(sync_db_path)
    spotify = FakeSpotify(scopes=READ_SCOPES)
    spotify.catalog["t1"] = ("Song", ["A"])
    svc = make_service(sync_db_path, spotify)
    await svc.process_message(GUILD, CHANNEL, 101, track_url("t1"), ALICE)
    assert window_ids(sync_db_path) == []

    # Billy re-consents; the same message is swept again by the rescan.
    spotify.scopes = set(FULL_SCOPES)
    summary = await svc.process_message(
        GUILD, CHANNEL, 101, track_url("t1"), ALICE
    )
    assert summary.skipped is False
    assert summary.added_track_ids == ["t1"]
    assert window_ids(sync_db_path) == ["t1"]
    with open_db(sync_db_path) as conn:
        assert store.is_message_processed(conn, GUILD, 101)


async def test_resolve_failure_stays_re_firable(sync_db_path):
    """A transient resolve error must not silently eat a valid link.

    A Spotify link that fails to resolve produces no track row AND no review
    queue entry, so if the message ledgers as processed the link is gone for
    good — a 30-second blip becomes permanent data loss.
    """
    seed_settings(sync_db_path)
    spotify = FakeSpotify()
    spotify.catalog["t1"] = ("Song", ["A"])
    spotify.resolve_error = SpotifyResolveError(
        "Spotify rate-limited; gave up after retries"
    )
    svc = make_service(sync_db_path, spotify)
    summary = await svc.process_message(GUILD, CHANNEL, 101, track_url("t1"), ALICE)
    assert summary.errors
    assert window_ids(sync_db_path) == []
    assert pending_rows(sync_db_path) == []  # not queued for review either
    with open_db(sync_db_path) as conn:
        assert not store.is_message_processed(conn, GUILD, 101)

    # Spotify recovers; the retry lands the track.
    spotify.resolve_error = None
    summary = await svc.process_message(GUILD, CHANNEL, 101, track_url("t1"), ALICE)
    assert summary.added_track_ids == ["t1"]
    with open_db(sync_db_path) as conn:
        assert store.is_message_processed(conn, GUILD, 101)


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(
            "Spotify refused the playlist write (403) — the authorized "
            "account likely doesn't own this playlist or can't edit it.",
            id="403",
        ),
        pytest.param("Spotify rate-limited; gave up after retries", id="429"),
    ],
)
async def test_spotify_write_error_tolerated(sync_db_path, message):
    seed_settings(sync_db_path)
    spotify = FakeSpotify()
    spotify.catalog["t1"] = ("Song", ["A"])
    spotify.add_error = SpotifyResolveError(message)
    svc = make_service(sync_db_path, spotify)
    summary = await svc.process_message(GUILD, CHANNEL, 101, track_url("t1"), ALICE)
    assert summary.write_blocked is True
    assert summary.errors == [message]
    assert window_ids(sync_db_path) == []


async def test_trim_removal_failure_keeps_db_conservative(sync_db_path):
    seed_settings(sync_db_path, window_size=1)
    spotify = FakeSpotify()
    spotify.catalog["t1"] = ("Song 1", ["A"])
    spotify.catalog["t2"] = ("Song 2", ["A"])
    svc = make_service(sync_db_path, spotify)
    await svc.process_message(GUILD, CHANNEL, 101, track_url("t1"), ALICE)
    spotify.remove_error = SpotifyResolveError(
        "Spotify rate-limited; gave up after retries"
    )
    summary = await svc.process_message(GUILD, CHANNEL, 102, track_url("t2"), ALICE)
    # The add landed; the trim removal failed but the DB already rolled t1
    # off — Spotify is over-full, never the other way around.
    assert summary.added_track_ids == ["t2"]
    assert summary.rolled_off_track_ids == ["t1"]
    assert summary.errors
    assert window_ids(sync_db_path) == ["t2"]


# ── Deletions ─────────────────────────────────────────────────────────


async def test_delete_removes_when_last_referrer(sync_db_path):
    seed_settings(sync_db_path)
    spotify = FakeSpotify()
    spotify.catalog["t1"] = ("Song", ["A"])
    svc = make_service(sync_db_path, spotify)
    await svc.process_message(GUILD, CHANNEL, 101, track_url("t1"), ALICE)
    result = await svc.handle_message_deleted(GUILD, 101)
    assert result.removed_track_ids == ["t1"]
    assert result.errors == []
    assert window_ids(sync_db_path) == []
    assert spotify.remove_calls == [(PLAYLIST, ["t1"])]


async def test_delete_is_inert_while_paused(sync_db_path):
    """Paused means paused. Tidying an old post must not strip the playlist.

    The Enabled toggle is documented as "stops processing without losing
    anything"; honouring only remove_on_delete let a paused feature keep
    deleting tracks whenever someone cleaned up the channel.
    """
    seed_settings(sync_db_path)
    spotify = FakeSpotify()
    spotify.catalog["t1"] = ("Song", ["A"])
    svc = make_service(sync_db_path, spotify)
    await svc.process_message(GUILD, CHANNEL, 101, track_url("t1"), ALICE)
    seed_settings(sync_db_path, enabled=False)

    result = await svc.handle_message_deleted(GUILD, 101)
    assert result.removed_track_ids == []
    assert spotify.remove_calls == []
    assert window_ids(sync_db_path) == ["t1"]  # still in the window


async def test_delete_keeps_track_with_other_live_referrer(sync_db_path):
    seed_settings(sync_db_path)
    spotify = FakeSpotify()
    spotify.catalog["t1"] = ("Song", ["A"])
    svc = make_service(sync_db_path, spotify)
    await svc.process_message(GUILD, CHANNEL, 101, track_url("t1"), ALICE)
    # Bob posts the same song: recorded as a duplicate reference (🔁).
    await svc.process_message(GUILD, CHANNEL, 102, track_url("t1"), BOB)
    result = await svc.handle_message_deleted(GUILD, 101)
    # Deleting Alice's post doesn't revoke Bob's — the track stays live,
    # re-attributed to his message.
    assert result.removed_track_ids == []
    assert not spotify.remove_calls
    with open_db(sync_db_path) as conn:
        (row,) = store.live_window(conn, GUILD, PLAYLIST)
        assert row["track_id"] == "t1"
        assert row["message_id"] == 102
        assert row["added_by"] == BOB


async def test_delete_respects_remove_on_delete_dial(sync_db_path):
    seed_settings(sync_db_path, remove_on_delete=False)
    spotify = FakeSpotify()
    spotify.catalog["t1"] = ("Song", ["A"])
    svc = make_service(sync_db_path, spotify)
    await svc.process_message(GUILD, CHANNEL, 101, track_url("t1"), ALICE)
    result = await svc.handle_message_deleted(GUILD, 101)
    assert result.removed_track_ids == []
    assert window_ids(sync_db_path) == ["t1"]


# ── Reconcile: square Spotify with the DB window ──────────────────────


async def test_reconcile_requires_a_playlist(sync_db_path):
    seed_settings(sync_db_path, playlist_id="")
    svc = make_service(sync_db_path, FakeSpotify())
    result = await svc.reconcile(GUILD)
    assert result == {"ok": False, "error": "No playlist configured."}


async def test_reconcile_adds_missing_and_removes_extras(sync_db_path):
    seed_settings(sync_db_path)
    spotify = FakeSpotify()
    for tid in ("t1", "t2"):
        spotify.catalog[tid] = (f"Song {tid}", ["A"])
    svc = make_service(sync_db_path, spotify)
    await svc.process_message(GUILD, CHANNEL, 101, track_url("t1"), ALICE)
    await svc.process_message(GUILD, CHANNEL, 102, track_url("t2"), BOB)
    # Drift both ways: t2's write never landed, and a stray track appeared.
    spotify.playlists[PLAYLIST] = ["t1", "tStray"]
    spotify.add_calls.clear()
    result = await svc.reconcile(GUILD)
    assert result == {"ok": True, "in_window": 2, "added": 1, "removed": 1}
    assert spotify.add_calls == [(PLAYLIST, ["t2"])]
    assert spotify.remove_calls == [(PLAYLIST, ["tStray"])]
    assert sorted(spotify.playlists[PLAYLIST]) == ["t1", "t2"]


async def test_reconcile_in_sync_is_a_no_op(sync_db_path):
    seed_settings(sync_db_path)
    spotify = FakeSpotify()
    spotify.catalog["t1"] = ("Song", ["A"])
    svc = make_service(sync_db_path, spotify)
    await svc.process_message(GUILD, CHANNEL, 101, track_url("t1"), ALICE)
    spotify.add_calls.clear()
    result = await svc.reconcile(GUILD)
    assert result == {"ok": True, "in_window": 1, "added": 0, "removed": 0}
    assert spotify.add_calls == [] and spotify.remove_calls == []


async def test_reconcile_withholds_a_bulk_delete_until_confirmed(sync_db_path):
    """Pointing the playlist dial at an existing playlist must not strip it.

    "On Spotify but not in the window" also describes every song on a playlist
    the bot never filled, and the removal is a real, irreversible write — so
    past a few strays the removals are withheld and reported for confirmation.
    """
    seed_settings(sync_db_path)
    spotify = FakeSpotify()
    spotify.catalog["t1"] = ("Song", ["A"])
    svc = make_service(sync_db_path, spotify)
    await svc.process_message(GUILD, CHANNEL, 101, track_url("t1"), ALICE)
    # Billy's own playlist, full of songs the bot didn't add.
    strays = [f"pre{i}" for i in range(12)]
    spotify.playlists[PLAYLIST] = ["t1", *strays]
    spotify.remove_calls.clear()

    result = await svc.reconcile(GUILD)
    assert result["ok"] is True
    assert result["removed"] == 0
    assert result["needs_confirmation"]["would_remove"] == 12
    assert len(result["needs_confirmation"]["sample"]) == 10
    assert spotify.remove_calls == []  # nothing was touched
    assert sorted(spotify.playlists[PLAYLIST]) == sorted(["t1", *strays])

    # Confirmed explicitly, it goes through.
    result = await svc.reconcile(GUILD, confirm_removals=True)
    assert result["removed"] == 12
    assert "needs_confirmation" not in result
    assert spotify.playlists[PLAYLIST] == ["t1"]


async def test_reconcile_still_heals_ordinary_drift_unprompted(sync_db_path):
    """A stray or two is real drift, not a wrong playlist — no prompt."""
    seed_settings(sync_db_path)
    spotify = FakeSpotify()
    spotify.catalog["t1"] = ("Song", ["A"])
    svc = make_service(sync_db_path, spotify)
    await svc.process_message(GUILD, CHANNEL, 101, track_url("t1"), ALICE)
    spotify.playlists[PLAYLIST] = ["t1", "stray1", "stray2"]
    result = await svc.reconcile(GUILD)
    assert result["removed"] == 2
    assert "needs_confirmation" not in result


async def test_reconcile_surfaces_spotify_errors(sync_db_path):
    seed_settings(sync_db_path)
    spotify = FakeSpotify()
    spotify.read_error = SpotifyResolveError("Spotify API error: 500 boom")
    svc = make_service(sync_db_path, spotify)
    result = await svc.reconcile(GUILD)
    assert result == {"ok": False, "error": "Spotify API error: 500 boom"}


# ── Review queue: approve / reject ────────────────────────────────────


def _queue_item(db_path, *, track_id="tRick", name="Never Gonna Give You Up",
                artist="Rick Astley", message_id=201) -> int:
    with open_db(db_path) as conn:
        return store.create_unmatched(
            conn, GUILD,
            channel_id=CHANNEL, message_id=message_id, source_url=YT_URL,
            added_by=ALICE, extracted_title=GOOD_META.title,
            extracted_channel=GOOD_META.channel_name,
            candidate_track_id=track_id, candidate_name=name,
            candidate_artist=artist, confidence=0.61,
            reason=REASON_LOW_CONFIDENCE,
        )


async def test_approve_adds_track_and_trims(sync_db_path):
    seed_settings(sync_db_path, window_size=1)
    spotify = FakeSpotify()
    spotify.catalog["t1"] = ("Old Song", ["A"])
    svc = make_service(sync_db_path, spotify)
    await svc.process_message(GUILD, CHANNEL, 101, track_url("t1"), ALICE)
    item_id = _queue_item(sync_db_path)
    result = await svc.approve_unmatched(GUILD, item_id, MOD)
    assert result.ok is True
    assert result.added_track_id == "tRick"
    assert result.was_duplicate is False
    assert window_ids(sync_db_path) == ["tRick"]  # t1 rolled off the 1-window
    assert spotify.remove_calls == [(PLAYLIST, ["t1"])]
    assert pending_rows(sync_db_path) == []
    with open_db(sync_db_path) as conn:
        row = store.live_window(conn, GUILD, PLAYLIST)[0]
        assert row["added_by"] == ALICE  # attributed to the poster, not the mod


async def test_approve_of_already_live_track_is_duplicate(sync_db_path):
    seed_settings(sync_db_path)
    spotify = FakeSpotify()
    spotify.catalog["tRick"] = ("Never Gonna Give You Up", ["Rick Astley"])
    svc = make_service(sync_db_path, spotify)
    await svc.process_message(GUILD, CHANNEL, 101, track_url("tRick"), BOB)
    item_id = _queue_item(sync_db_path)
    add_calls_before = len(spotify.add_calls)
    result = await svc.approve_unmatched(GUILD, item_id, MOD)
    assert result.ok is True
    assert result.was_duplicate is True
    assert len(spotify.add_calls) == add_calls_before  # no second write
    assert window_ids(sync_db_path) == ["tRick"]


async def test_approve_write_failure_reopens_item(sync_db_path):
    seed_settings(sync_db_path)
    spotify = FakeSpotify(scopes=READ_SCOPES)
    svc = make_service(sync_db_path, spotify)
    item_id = _queue_item(sync_db_path)
    result = await svc.approve_unmatched(GUILD, item_id, MOD)
    assert result.ok is False
    assert "read-only" in result.detail
    # The claim was rolled back: the item is pending again, retryable.
    assert [r["id"] for r in pending_rows(sync_db_path)] == [item_id]
    assert window_ids(sync_db_path) == []


@pytest.mark.parametrize(
    ("item_kwargs", "detail_fragment"),
    [
        pytest.param({"track_id": None, "name": None, "artist": None},
                     "No candidate track", id="no-candidate"),
    ],
)
async def test_approve_guards(sync_db_path, item_kwargs, detail_fragment):
    seed_settings(sync_db_path)
    svc = make_service(sync_db_path, FakeSpotify())
    item_id = _queue_item(sync_db_path, **item_kwargs)
    result = await svc.approve_unmatched(GUILD, item_id, MOD)
    assert result.ok is False
    assert detail_fragment in result.detail


async def test_approve_missing_item_and_already_resolved(sync_db_path):
    seed_settings(sync_db_path)
    svc = make_service(sync_db_path, FakeSpotify())
    missing = await svc.approve_unmatched(GUILD, 9999, MOD)
    assert missing.ok is False and "not found" in missing.detail
    item_id = _queue_item(sync_db_path)
    assert await svc.reject_unmatched(GUILD, item_id, MOD) is True
    again = await svc.approve_unmatched(GUILD, item_id, MOD)
    assert again.ok is False
    assert "rejected" in again.detail


async def test_reject_is_one_way(sync_db_path):
    seed_settings(sync_db_path)
    svc = make_service(sync_db_path, FakeSpotify())
    item_id = _queue_item(sync_db_path)
    assert await svc.reject_unmatched(GUILD, item_id, MOD) is True
    assert await svc.reject_unmatched(GUILD, item_id, MOD) is False
    assert pending_rows(sync_db_path) == []


# ── Connection status ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("scopes", "expected"),
    [
        pytest.param(FULL_SCOPES, "connected", id="full-grant"),
        pytest.param(READ_SCOPES, "read_only", id="read-only"),
        pytest.param(set(), "not_connected", id="never-authorized"),
    ],
)
async def test_connection_status(sync_db_path, scopes, expected):
    svc = make_service(sync_db_path, FakeSpotify(scopes=scopes))
    assert await svc.connection_status() == expected


# ── oEmbed fetch (mock transport — no network) ────────────────────────


async def test_fetch_youtube_metadata_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oembed"
        assert request.url.params["url"].endswith(VIDEO_ID)
        return httpx.Response(
            200, json={"title": "Artist - Song", "author_name": "Artist"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        meta = await fetch_youtube_metadata(VIDEO_ID, client=client)
    assert meta is not None
    assert meta.title == "Artist - Song"
    assert meta.channel_name == "Artist"
    assert meta.video_id == VIDEO_ID


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(404), id="404"),
        pytest.param(httpx.Response(401), id="4xx"),
        pytest.param(httpx.Response(200, json={"title": ""}), id="no-title"),
    ],
)
async def test_fetch_youtube_metadata_unavailable(response):
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await fetch_youtube_metadata(VIDEO_ID, client=client) is None


# ── Spotify search helper ─────────────────────────────────────────────


class _FakeSearchResolver:
    """Just enough resolver surface for search_spotify_tracks."""

    def __init__(self, payload):
        self._payload = payload
        self.calls: list[dict] = []

    def _ensure_client(self):
        return self

    def search(self, **kwargs):  # pragma: no cover - never actually invoked
        raise AssertionError("search should go through _call")

    async def _call(self, fn, **kwargs):
        self.calls.append(kwargs)
        return self._payload


async def test_search_spotify_tracks_parses_payload():
    payload = {
        "tracks": {
            "items": [
                {
                    "id": "t1",
                    "name": "Song",
                    "artists": [{"name": "A"}, {"name": "B"}],
                    "external_urls": {"spotify": track_url("t1")},
                },
                {"id": None, "name": "local file", "artists": []},
            ]
        }
    }
    resolver = _FakeSearchResolver(payload)
    tracks = await search_spotify_tracks(resolver, "song a", limit=5)  # type: ignore[arg-type]
    assert tracks == [
        SpotifyTrack(track_id="t1", name="Song", artists=["A", "B"],
                     url=track_url("t1"))
    ]
    assert resolver.calls == [{"q": "song a", "type": "track", "limit": 5}]
