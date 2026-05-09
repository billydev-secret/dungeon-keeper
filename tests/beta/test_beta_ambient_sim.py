"""Tests for beta_tools.ambient_sim.AmbientSim."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch


from beta_tools.config import BetaConfig
from beta_tools.personas import Persona


def _make_persona(key: str, weight: float = 1.0, affinities: dict | None = None) -> Persona:
    return Persona(
        key=key, display_name=key.capitalize(), avatar_url="https://x/a.png",
        activity_weight=weight,
        channel_affinities=affinities or {"general": 1.0},
        voice_likely=False, message_length_bias="short",
    )


def _make_handle(key: str, weight: float = 1.0, affinities: dict | None = None):
    from beta_tools.puppet_manager import PuppetHandle
    handle = PuppetHandle(
        key=key,
        persona=_make_persona(key, weight, affinities),
        token="t",
        expected_id=1,
    )
    handle.client = MagicMock()
    handle.ready = MagicMock()
    handle.ready.is_set = MagicMock(return_value=True)
    fake_channel = MagicMock()
    fake_channel.send = AsyncMock()
    handle.client.get_channel = MagicMock(return_value=fake_channel)
    return handle


def _make_guild(channel_names: list[str] | None = None) -> MagicMock:
    guild = MagicMock()
    names = channel_names or ["general"]
    channels = []
    for i, name in enumerate(names):
        ch = MagicMock()
        ch.name = name
        ch.id = 100 + i
        channels.append(ch)
    guild.text_channels = channels
    return guild


def _make_beta_cfg(rate_multiplier: float = 1.0) -> BetaConfig:
    return BetaConfig(
        tools_token="t", tools_expected_id=1,
        puppet_tokens=("p1", "p2", "p3"),
        puppet_expected_ids=(1, 2, 3),
        enabled=True,
        ambient_rate_multiplier=rate_multiplier,
        ambient_autostart=False,
        llm_blend=False,
    )


def _make_sim(handles=None, guild=None, rate_multiplier: float = 1.0):
    from beta_tools.ambient_sim import AmbientSim
    from beta_tools.markov import MarkovChain

    chain = MagicMock(spec=MarkovChain)
    chain.generate = MagicMock(return_value="hello world today")
    chain.corpus_size = 500
    chain.vocab_size = 200

    pm = MagicMock()
    pm.handles = handles or [
        _make_handle("alice"),
        _make_handle("bob"),
        _make_handle("clara"),
    ]

    return AmbientSim(
        chain=chain,
        puppet_manager=pm,
        guild=guild or _make_guild(),
        beta_cfg=_make_beta_cfg(rate_multiplier),
    )


# ── State properties ─────────────────────────────────────────────────────────

def test_not_running_initially():
    sim = _make_sim()
    assert sim.is_running is False


def test_posts_since_start_zero_initially():
    sim = _make_sim()
    assert sim.posts_since_start == 0


def test_last_post_none_initially():
    sim = _make_sim()
    assert sim.last_post is None


# ── start / stop lifecycle ───────────────────────────────────────────────────

async def test_start_sets_is_running():
    sim = _make_sim()
    sim.start()
    assert sim.is_running is True
    await sim.stop()


async def test_start_twice_does_not_create_second_task():
    sim = _make_sim()
    sim.start()
    task1 = sim._task
    sim.start()
    assert sim._task is task1
    await sim.stop()


async def test_stop_cancels_task():
    sim = _make_sim()
    sim.start()
    assert sim.is_running is True
    await sim.stop()
    assert sim.is_running is False


async def test_stop_when_not_running_is_safe():
    sim = _make_sim()
    await sim.stop()  # must not raise
    assert sim.is_running is False


# ── Channel resolution ───────────────────────────────────────────────────────

def test_resolve_channel_finds_by_name():
    sim = _make_sim(guild=_make_guild(["general", "random"]))
    ch = sim._resolve_channel("general")
    assert ch is not None
    assert ch.name == "general"


def test_resolve_channel_returns_none_for_missing():
    sim = _make_sim(guild=_make_guild(["general"]))
    ch = sim._resolve_channel("drama")
    assert ch is None


def test_resolve_channel_warns_only_once(caplog):
    import logging
    sim = _make_sim(guild=_make_guild(["general"]))
    with caplog.at_level(logging.WARNING, logger="beta_tools.ambient_sim"):
        sim._resolve_channel("missing")
        sim._resolve_channel("missing")
    warnings = [r for r in caplog.records if "missing" in r.message]
    assert len(warnings) == 1


# ── Puppet and channel selection ─────────────────────────────────────────────

def test_pick_puppet_respects_weight():
    alice = _make_handle("alice", weight=0.01)
    bob = _make_handle("bob", weight=10.0)
    clara = _make_handle("clara", weight=0.01)
    sim = _make_sim(handles=[alice, bob, clara])
    picks = [sim._pick_puppet().key for _ in range(200)]
    assert picks.count("bob") > 150


def test_pick_channel_returns_resolvable_channel():
    handle = _make_handle("alice", affinities={"general": 1.0})
    sim = _make_sim(handles=[handle, _make_handle("bob"), _make_handle("clara")])
    result = sim._pick_channel(handle)
    assert result is not None
    ch, name = result
    assert name == "general"


def test_pick_channel_skips_unresolvable_names():
    handle = _make_handle("alice", affinities={"nonexistent": 0.9, "general": 0.1})
    sim = _make_sim(handles=[handle, _make_handle("bob"), _make_handle("clara")])
    result = sim._pick_channel(handle)
    assert result is not None
    ch, name = result
    assert name == "general"


def test_pick_channel_returns_none_when_all_unresolvable():
    handle = _make_handle("alice", affinities={"nowhere": 1.0})
    sim = _make_sim(handles=[handle, _make_handle("bob"), _make_handle("clara")])
    result = sim._pick_channel(handle)
    assert result is None


# ── Sleep duration ───────────────────────────────────────────────────────────

def test_sleep_duration_burst_window():
    from beta_tools.ambient_sim import _BURST_INTERVAL
    sim = _make_sim()
    sim._last_post_at = time.monotonic()
    assert sim._sleep_duration() == _BURST_INTERVAL


def test_sleep_duration_base_outside_burst():
    from beta_tools.ambient_sim import _BASE_INTERVAL, _BURST_DURATION
    sim = _make_sim(rate_multiplier=1.0)
    sim._last_post_at = time.monotonic() - _BURST_DURATION - 1
    duration = sim._sleep_duration()
    assert _BASE_INTERVAL * 0.75 <= duration <= _BASE_INTERVAL * 1.25


def test_sleep_duration_scales_with_rate_multiplier():
    from beta_tools.ambient_sim import _BASE_INTERVAL, _BURST_DURATION
    sim = _make_sim(rate_multiplier=2.0)
    sim._last_post_at = time.monotonic() - _BURST_DURATION - 1
    duration = sim._sleep_duration()
    expected = _BASE_INTERVAL / 2.0
    assert expected * 0.75 <= duration <= expected * 1.25


# ── _tick logic ───────────────────────────────────────────────────────────────

async def test_tick_sends_message_and_increments_posts():
    sim = _make_sim()
    handle = sim._puppet_manager.handles[0]
    assert handle.client is not None
    # _make_handle sets get_channel = MagicMock(return_value=fake_channel) where fake_channel.send = AsyncMock
    get_channel_mock: MagicMock = handle.client.get_channel  # type: ignore[assignment]
    puppet_channel = get_channel_mock.return_value

    with patch.object(sim, "_pick_puppet", return_value=handle):
        await sim._tick()

    puppet_channel.send.assert_awaited_once_with("hello world today")
    assert sim.posts_since_start == 1
    last = sim.last_post
    assert last is not None
    key, ch_name, ts = last
    assert key == handle.key
    assert ch_name == "general"


async def test_tick_skips_unready_puppet():
    sim = _make_sim()
    handle = sim._puppet_manager.handles[0]
    handle.ready.is_set = MagicMock(return_value=False)

    with patch.object(sim, "_pick_puppet", return_value=handle):
        await sim._tick()

    assert sim.posts_since_start == 0


async def test_tick_skips_when_no_resolvable_channel():
    handle = _make_handle("alice", affinities={"nowhere": 1.0})
    sim = _make_sim(handles=[handle, _make_handle("bob"), _make_handle("clara")])

    with patch.object(sim, "_pick_puppet", return_value=handle):
        await sim._tick()

    assert sim.posts_since_start == 0


async def test_tick_skips_when_puppet_cannot_see_channel():
    sim = _make_sim()
    handle = sim._puppet_manager.handles[0]
    assert handle.client is not None
    handle.client.get_channel = MagicMock(return_value=None)

    with patch.object(sim, "_pick_puppet", return_value=handle):
        await sim._tick()

    assert sim.posts_since_start == 0
