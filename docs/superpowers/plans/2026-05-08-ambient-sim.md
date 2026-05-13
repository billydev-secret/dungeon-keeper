# Ambient Sim Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an ambient chat simulation loop to the beta_tools sidecar so the three puppet bots post realistic messages in dev-guild channels automatically, driven by a bigram Markov chain built from real user messages.

**Architecture:** A one-shot `scripts/build_markov.py` builds `fixtures/markov_chain.json` from any messages DB. `beta_tools/markov.py` loads that file and generates text. `beta_tools/ambient_sim.py` contains the `AmbientSim` dispatcher that picks puppet/channel/message each tick, with a burst window after each post. Three new slash commands start/stop/status the loop. `DkToolsBot.on_ready` instantiates `AmbientSim` and auto-starts it if `BETA_AMBIENT_AUTOSTART=1`.

**Tech Stack:** Python 3.10+, discord.py 2.x, sqlite3 (corpus build only), asyncio, pytest with `asyncio_mode = auto`.

---

## File Map

| Action | Path | Purpose |
|--------|------|---------|
| Create | `scripts/build_markov.py` | One-shot: read messages DB → write `fixtures/markov_chain.json` |
| Create | `beta_tools/markov.py` | `MarkovChain` class: load JSON, generate text |
| Create | `beta_tools/ambient_sim.py` | `AmbientSim` class: dispatcher loop |
| Create | `beta_tools/slash/ambient.py` | `/beta-ambient-start/stop/status` handlers |
| Create | `tests/beta/test_beta_markov.py` | Unit tests for `MarkovChain` |
| Create | `tests/beta/test_beta_ambient_sim.py` | Unit tests for `AmbientSim` |
| Create | `tests/beta/test_beta_slash_ambient.py` | Unit tests for ambient slash handlers |
| Modify | `beta_tools/bot.py` | Load chain in `setup_hook`, instantiate sim in `on_ready`, stop in `close` |
| Modify | `beta_tools/slash/__init__.py` | Register ambient commands |
| Modify | `beta_tools/slash/help.py` | Add "Ambient Sim" field |

---

## Task 1: Corpus Builder Script

**Files:**
- Create: `scripts/build_markov.py`

- [ ] **Step 1: Write the script**

```python
# scripts/build_markov.py
#!/usr/bin/env python3
"""Build a bigram Markov chain from the messages table and write it to JSON.

Usage:
    python scripts/build_markov.py --db path/to/dk.db --out fixtures/markov_chain.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

MIN_CORPUS = 100


def _load_messages(db_path: str) -> list[str]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT m.content
            FROM messages m
            LEFT JOIN known_users ku
                ON m.author_id = ku.user_id AND m.guild_id = ku.guild_id
            WHERE m.content IS NOT NULL
              AND m.source IS NULL
              AND (ku.is_bot IS NULL OR ku.is_bot = 0)
            """
        ).fetchall()
    finally:
        con.close()
    return [r["content"] for r in rows]


def _build_chain(messages: list[str]) -> dict[str, list[str]]:
    chain: dict[str, list[str]] = defaultdict(list)
    for msg in messages:
        words = msg.split()
        if len(words) < 3:
            continue
        for i in range(len(words) - 2):
            key = f"{words[i]} {words[i + 1]}"
            chain[key].append(words[i + 2])
    return dict(chain)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build bigram Markov chain from messages DB")
    parser.add_argument("--db", required=True, help="Path to SQLite DB with messages table")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    messages = _load_messages(args.db)
    valid = [m for m in messages if len(m.split()) >= 3]
    print(f"Loaded {len(messages)} messages, {len(valid)} usable (>=3 words)")

    if len(valid) < MIN_CORPUS:
        raise SystemExit(
            f"Only {len(valid)} usable messages -- need at least {MIN_CORPUS}. "
            "Point --db at a richer database."
        )

    chain = _build_chain(valid)
    out_data = {
        "version": 1,
        "corpus_size": len(valid),
        "chain": chain,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(chain)} bigram states to {out_path}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the script against the dev DB**

```
python scripts/build_markov.py --db dk_dev.db --out fixtures/markov_chain.json
```

Expected output (numbers will vary):
```
Loaded 8432 messages, 7218 usable (>=3 words)
Wrote 4891 bigram states to fixtures/markov_chain.json
```

If you see `Only N usable messages -- need at least 100`, point it at the prod DB instead:
```
python scripts/build_markov.py --db dungeonkeeper.db --out fixtures/markov_chain.json
```

- [ ] **Step 3: Verify the output is valid JSON with the expected shape**

```
python -c "
import json
d = json.load(open('fixtures/markov_chain.json'))
assert d['version'] == 1
assert d['corpus_size'] > 0
assert len(d['chain']) > 0
first_key = next(iter(d['chain']))
assert len(first_key.split()) == 2, 'keys must be bigrams'
print(f'OK: {d[\"corpus_size\"]} messages, {len(d[\"chain\"])} bigrams')
"
```

Expected: `OK: <N> messages, <M> bigrams`

- [ ] **Step 4: Commit**

```bash
git add scripts/build_markov.py fixtures/markov_chain.json
git commit -m "feat(ambient-sim): corpus builder script + initial markov_chain.json"
```

---

## Task 2: MarkovChain Class

**Files:**
- Create: `beta_tools/markov.py`
- Create: `tests/beta/test_beta_markov.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/beta/test_beta_markov.py
"""Tests for beta_tools.markov.MarkovChain."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _make_chain_file(tmp_path: Path, chain: dict, corpus_size: int = 500) -> Path:
    data = {"version": 1, "corpus_size": corpus_size, "chain": chain}
    p = tmp_path / "chain.json"
    p.write_text(json.dumps(data))
    return p


def _cyclic_chain(n: int) -> dict[str, list[str]]:
    """Build an n-word cyclic chain so generation never dead-ends."""
    words = [f"word{i}" for i in range(n)]
    chain = {}
    for i in range(n - 2):
        chain[f"{words[i]} {words[i+1]}"] = [words[i + 2]]
    chain[f"{words[-2]} {words[-1]}"] = [words[0]]
    return chain


def test_load_sets_corpus_size(tmp_path):
    from beta_tools.markov import MarkovChain
    p = _make_chain_file(tmp_path, _cyclic_chain(10), corpus_size=999)
    mc = MarkovChain.load(p)
    assert mc.corpus_size == 999


def test_load_sets_vocab_size(tmp_path):
    from beta_tools.markov import MarkovChain
    chain = _cyclic_chain(10)
    p = _make_chain_file(tmp_path, chain)
    mc = MarkovChain.load(p)
    assert mc.vocab_size == len(chain)


def test_generate_returns_nonempty_string(tmp_path):
    from beta_tools.markov import MarkovChain
    p = _make_chain_file(tmp_path, _cyclic_chain(20))
    mc = MarkovChain.load(p)
    result = mc.generate("short")
    assert isinstance(result, str)
    assert len(result) > 0


def test_generate_short_within_word_range(tmp_path):
    from beta_tools.markov import MarkovChain
    p = _make_chain_file(tmp_path, _cyclic_chain(50))
    mc = MarkovChain.load(p)
    for _ in range(20):
        result = mc.generate("short")
        word_count = len(result.split())
        assert 5 <= word_count <= 15, f"Expected 5-15 words, got {word_count}: {result!r}"


def test_generate_medium_within_word_range(tmp_path):
    from beta_tools.markov import MarkovChain
    p = _make_chain_file(tmp_path, _cyclic_chain(50))
    mc = MarkovChain.load(p)
    for _ in range(10):
        result = mc.generate("medium")
        word_count = len(result.split())
        assert 10 <= word_count <= 30, f"Expected 10-30 words, got {word_count}"


def test_generate_long_within_word_range(tmp_path):
    from beta_tools.markov import MarkovChain
    p = _make_chain_file(tmp_path, _cyclic_chain(100))
    mc = MarkovChain.load(p)
    for _ in range(10):
        result = mc.generate("long")
        word_count = len(result.split())
        assert 20 <= word_count <= 60, f"Expected 20-60 words, got {word_count}"


def test_generate_unknown_bias_falls_back_to_medium(tmp_path):
    from beta_tools.markov import MarkovChain
    p = _make_chain_file(tmp_path, _cyclic_chain(50))
    mc = MarkovChain.load(p)
    for _ in range(10):
        result = mc.generate("nonexistent_bias")
        word_count = len(result.split())
        assert 10 <= word_count <= 30


def test_generate_handles_dead_end_chain(tmp_path):
    from beta_tools.markov import MarkovChain
    # A chain that immediately dead-ends
    dead_end = {"hello world": []}
    p = _make_chain_file(tmp_path, dead_end)
    mc = MarkovChain.load(p)
    result = mc.generate("short")
    assert isinstance(result, str)


def test_generate_stops_at_max_budget(tmp_path):
    from beta_tools.markov import MarkovChain
    # Infinite cyclic chain — generation must stop at budget
    p = _make_chain_file(tmp_path, _cyclic_chain(100))
    mc = MarkovChain.load(p)
    result = mc.generate("short")
    assert len(result.split()) <= 15
```

- [ ] **Step 2: Run tests — expect failure**

```
pytest tests/beta/test_beta_markov.py -v
```

Expected: `ModuleNotFoundError: No module named 'beta_tools.markov'`

- [ ] **Step 3: Write the implementation**

```python
# beta_tools/markov.py
"""Bigram Markov chain for ambient sim message generation."""
from __future__ import annotations

import json
import random
from pathlib import Path

_LENGTH_RANGES: dict[str, tuple[int, int]] = {
    "short":  (5, 15),
    "medium": (10, 30),
    "long":   (20, 60),
}

_SENTENCE_ENDERS = {".", "!", "?"}


class MarkovChain:
    def __init__(self, chain: dict[str, list[str]], corpus_size: int) -> None:
        self._chain = chain
        self._corpus_size = corpus_size
        self._keys = list(chain.keys())

    @classmethod
    def load(cls, path: str | Path) -> "MarkovChain":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(chain=data["chain"], corpus_size=data["corpus_size"])

    @property
    def corpus_size(self) -> int:
        return self._corpus_size

    @property
    def vocab_size(self) -> int:
        return len(self._keys)

    def generate(self, length_bias: str = "medium") -> str:
        if length_bias not in _LENGTH_RANGES:
            length_bias = "medium"
        min_words, max_words = _LENGTH_RANGES[length_bias]

        if not self._keys:
            return ""

        state = random.choice(self._keys)
        words: list[str] = list(state.split())

        for _ in range(max_words - len(words)):
            followers = self._chain.get(state) or []
            if not followers:
                if len(words) >= min_words:
                    break
                state = random.choice(self._keys)
                words.extend(state.split())
                continue

            next_word = random.choice(followers)
            words.append(next_word)

            if len(words) >= min_words and next_word[-1] in _SENTENCE_ENDERS:
                break

            state = f"{words[-2]} {words[-1]}"

        return " ".join(words)
```

- [ ] **Step 4: Run tests — expect all pass**

```
pytest tests/beta/test_beta_markov.py -v
```

Expected: all 9 tests pass.

- [ ] **Step 5: Commit**

```bash
git add beta_tools/markov.py tests/beta/test_beta_markov.py
git commit -m "feat(ambient-sim): MarkovChain class with bigram generation"
```

---

## Task 3: AmbientSim Class

**Files:**
- Create: `beta_tools/ambient_sim.py`
- Create: `tests/beta/test_beta_ambient_sim.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/beta/test_beta_ambient_sim.py
"""Tests for beta_tools.ambient_sim.AmbientSim."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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

def test_start_sets_is_running():
    sim = _make_sim()
    sim.start()
    assert sim.is_running is True
    sim._task.cancel()


def test_start_twice_does_not_create_second_task():
    sim = _make_sim()
    sim.start()
    task1 = sim._task
    sim.start()
    assert sim._task is task1
    task1.cancel()


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
    puppet_channel = handle.client.get_channel.return_value

    with patch.object(sim, "_pick_puppet", return_value=handle):
        await sim._tick()

    puppet_channel.send.assert_awaited_once_with("hello world today")
    assert sim.posts_since_start == 1
    key, ch_name, ts = sim.last_post
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
    handle.client.get_channel = MagicMock(return_value=None)

    with patch.object(sim, "_pick_puppet", return_value=handle):
        await sim._tick()

    assert sim.posts_since_start == 0
```

- [ ] **Step 2: Run tests — expect failure**

```
pytest tests/beta/test_beta_ambient_sim.py -v
```

Expected: `ModuleNotFoundError: No module named 'beta_tools.ambient_sim'`

- [ ] **Step 3: Write the implementation**

```python
# beta_tools/ambient_sim.py
"""AmbientSim — central dispatcher loop for puppet ambient traffic."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

import discord

from beta_tools.config import BetaConfig
from beta_tools.markov import MarkovChain
from beta_tools.puppet_manager import PuppetHandle, PuppetManager

log = logging.getLogger("beta_tools.ambient_sim")

_BASE_INTERVAL: float = 15.0
_BURST_INTERVAL: float = 5.0
_BURST_DURATION: float = 30.0
_JITTER: float = 0.2
_HTTP_ERROR_SLEEP: float = 10.0
_LOOP_ERROR_SLEEP: float = 30.0


class AmbientSim:
    def __init__(
        self,
        chain: MarkovChain,
        puppet_manager: PuppetManager,
        guild: discord.Guild,
        beta_cfg: BetaConfig,
    ) -> None:
        self._chain = chain
        self._puppet_manager = puppet_manager
        self._guild = guild
        self._beta_cfg = beta_cfg
        self._task: Optional[asyncio.Task] = None
        self._posts: int = 0
        self._last_post_at: float = 0.0
        self._last_post: Optional[tuple[str, str, float]] = None
        self._warned_channels: set[str] = set()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def posts_since_start(self) -> int:
        return self._posts

    @property
    def last_post(self) -> Optional[tuple[str, str, float]]:
        return self._last_post

    def start(self) -> None:
        if self.is_running:
            return
        self._posts = 0
        self._last_post_at = 0.0
        self._last_post = None
        self._warned_channels = set()
        self._task = asyncio.create_task(self._loop(), name="ambient-sim")
        log.info("ambient sim started (rate_multiplier=%.2f)", self._beta_cfg.ambient_rate_multiplier)

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        log.info("ambient sim stopped after %d posts", self._posts)

    def _base_interval(self) -> float:
        return _BASE_INTERVAL / max(self._beta_cfg.ambient_rate_multiplier, 0.1)

    def _sleep_duration(self) -> float:
        if time.monotonic() - self._last_post_at < _BURST_DURATION:
            return _BURST_INTERVAL
        base = self._base_interval()
        return max(1.0, base + base * random.uniform(-_JITTER, _JITTER))

    def _pick_puppet(self) -> PuppetHandle:
        handles = self._puppet_manager.handles
        weights = [h.persona.activity_weight for h in handles]
        return random.choices(handles, weights=weights, k=1)[0]

    def _resolve_channel(self, name: str) -> Optional[discord.TextChannel]:
        for ch in self._guild.text_channels:
            if ch.name == name:
                return ch
        if name not in self._warned_channels:
            log.warning("ambient sim: channel %r not found in guild (will not warn again)", name)
            self._warned_channels.add(name)
        return None

    def _pick_channel(self, handle: PuppetHandle) -> Optional[tuple[discord.TextChannel, str]]:
        affinities = handle.persona.channel_affinities
        names = list(affinities.keys())
        weights = [affinities[n] for n in names]
        seen: set[int] = set()
        for idx in random.choices(range(len(names)), weights=weights, k=len(names)):
            if idx in seen:
                continue
            seen.add(idx)
            ch = self._resolve_channel(names[idx])
            if ch is not None:
                return ch, names[idx]
        return None

    async def _tick(self) -> None:
        handle = self._pick_puppet()

        if handle.client is None or not handle.ready.is_set():
            log.debug("puppet %r not ready, skipping tick", handle.key)
            return

        result = self._pick_channel(handle)
        if result is None:
            log.debug("no resolvable channels for puppet %r, skipping tick", handle.key)
            return
        channel, channel_name = result

        puppet_channel = handle.client.get_channel(channel.id)
        if puppet_channel is None:
            log.debug("puppet %r cannot see channel %r, skipping", handle.key, channel_name)
            return

        text = self._chain.generate(handle.persona.message_length_bias)
        if not text:
            return

        await puppet_channel.send(text)
        self._last_post_at = time.monotonic()
        self._last_post = (handle.key, channel_name, time.time())
        self._posts += 1
        log.debug("ambient sim: %r posted in #%s", handle.key, channel_name)

    async def _loop(self) -> None:
        log.info("ambient sim loop running")
        while True:
            try:
                await asyncio.sleep(self._sleep_duration())
                await self._tick()
            except asyncio.CancelledError:
                log.info("ambient sim loop cancelled")
                return
            except discord.Forbidden:
                log.warning("ambient sim: Forbidden error on tick — skipping")
            except discord.HTTPException as exc:
                log.warning("ambient sim: HTTP error %s — sleeping %.0fs", exc, _HTTP_ERROR_SLEEP)
                await asyncio.sleep(_HTTP_ERROR_SLEEP)
            except Exception:
                log.exception("ambient sim: unexpected error — sleeping %.0fs", _LOOP_ERROR_SLEEP)
                await asyncio.sleep(_LOOP_ERROR_SLEEP)
```

- [ ] **Step 4: Run tests — expect all pass**

```
pytest tests/beta/test_beta_ambient_sim.py -v
```

Expected: all 20 tests pass.

- [ ] **Step 5: Commit**

```bash
git add beta_tools/ambient_sim.py tests/beta/test_beta_ambient_sim.py
git commit -m "feat(ambient-sim): AmbientSim dispatcher loop with burst window"
```

---

## Task 4: Ambient Slash Commands

**Files:**
- Create: `beta_tools/slash/ambient.py`
- Create: `tests/beta/test_beta_slash_ambient.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/beta/test_beta_slash_ambient.py
"""Tests for /beta-ambient-* command handlers."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from beta_tools.config import BetaConfig


def _mod_interaction():
    interaction = MagicMock()
    role = MagicMock()
    role.name = "Mod"
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.roles = [role]
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


def _make_sim(*, running: bool = False, posts: int = 0):
    from beta_tools.ambient_sim import AmbientSim
    sim = MagicMock(spec=AmbientSim)
    sim.is_running = running
    sim.posts_since_start = posts
    sim.last_post = ("alice", "general", 1000.0) if running else None
    sim.start = MagicMock()
    sim.stop = AsyncMock()
    sim._base_interval = MagicMock(return_value=15.0)
    sim._chain = MagicMock()
    sim._chain.corpus_size = 500
    sim._chain.vocab_size = 200
    return sim


async def test_start_handler_starts_sim():
    from beta_tools.slash.ambient import _ambient_start_handler
    bot = MagicMock()
    bot.ambient_sim = _make_sim(running=False)
    interaction = _mod_interaction()

    await _ambient_start_handler(bot, interaction)

    bot.ambient_sim.start.assert_called_once()
    interaction.response.send_message.assert_awaited_once()
    msg = interaction.response.send_message.call_args.args[0]
    assert "started" in msg.lower()


async def test_start_handler_already_running():
    from beta_tools.slash.ambient import _ambient_start_handler
    bot = MagicMock()
    bot.ambient_sim = _make_sim(running=True)
    interaction = _mod_interaction()

    await _ambient_start_handler(bot, interaction)

    bot.ambient_sim.start.assert_not_called()
    msg = interaction.response.send_message.call_args.args[0]
    assert "already" in msg.lower()


async def test_stop_handler_stops_sim():
    from beta_tools.slash.ambient import _ambient_stop_handler
    bot = MagicMock()
    bot.ambient_sim = _make_sim(running=True, posts=42)
    interaction = _mod_interaction()

    await _ambient_stop_handler(bot, interaction)

    bot.ambient_sim.stop.assert_awaited_once()
    msg = interaction.response.send_message.call_args.args[0]
    assert "stopped" in msg.lower()
    assert "42" in msg


async def test_stop_handler_not_running():
    from beta_tools.slash.ambient import _ambient_stop_handler
    bot = MagicMock()
    bot.ambient_sim = _make_sim(running=False)
    interaction = _mod_interaction()

    await _ambient_stop_handler(bot, interaction)

    bot.ambient_sim.stop.assert_not_called()
    msg = interaction.response.send_message.call_args.args[0]
    assert "not running" in msg.lower()


async def test_status_handler_shows_running_state():
    from beta_tools.slash.ambient import _ambient_status_handler
    bot = MagicMock()
    bot.ambient_sim = _make_sim(running=True, posts=7)
    interaction = _mod_interaction()

    await _ambient_status_handler(bot, interaction)

    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs.get("ephemeral") is True
    msg = interaction.response.send_message.call_args.args[0]
    assert "7" in msg


async def test_status_handler_shows_stopped_state():
    from beta_tools.slash.ambient import _ambient_status_handler
    bot = MagicMock()
    bot.ambient_sim = _make_sim(running=False, posts=0)
    interaction = _mod_interaction()

    await _ambient_status_handler(bot, interaction)

    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs.get("ephemeral") is True


async def test_start_handler_rejects_non_mod():
    from beta_tools.slash.ambient import _ambient_start_handler
    bot = MagicMock()
    bot.ambient_sim = _make_sim(running=False)

    interaction = MagicMock()
    role = MagicMock()
    role.name = "Member"
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.roles = [role]
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()

    await _ambient_start_handler(bot, interaction)

    bot.ambient_sim.start.assert_not_called()
    msg = interaction.response.send_message.call_args.args[0]
    assert "moderator" in msg.lower()
```

- [ ] **Step 2: Run tests — expect failure**

```
pytest tests/beta/test_beta_slash_ambient.py -v
```

Expected: `ModuleNotFoundError: No module named 'beta_tools.slash.ambient'`

- [ ] **Step 3: Write the implementation**

```python
# beta_tools/slash/ambient.py
"""/beta-ambient-* slash commands — start, stop, status for the ambient sim."""
from __future__ import annotations

import time
import logging

import discord

from beta_tools.slash._base import reject_if_not_mod

log = logging.getLogger("beta_tools.slash.ambient")


async def _ambient_start_handler(bot, interaction: discord.Interaction) -> None:
    if not await reject_if_not_mod(interaction):
        return
    sim = bot.ambient_sim
    if sim.is_running:
        await interaction.response.send_message("Ambient sim is already running.", ephemeral=True)
        return
    sim.start()
    base = sim._base_interval()
    await interaction.response.send_message(
        f"Ambient sim started — base interval `{base:.0f}s`, burst `5s` for `30s` after each post.",
        ephemeral=True,
    )


async def _ambient_stop_handler(bot, interaction: discord.Interaction) -> None:
    if not await reject_if_not_mod(interaction):
        return
    sim = bot.ambient_sim
    if not sim.is_running:
        await interaction.response.send_message("Ambient sim is not running.", ephemeral=True)
        return
    posts = sim.posts_since_start
    await sim.stop()
    await interaction.response.send_message(
        f"Ambient sim stopped — `{posts}` post{'s' if posts != 1 else ''} sent this session.",
        ephemeral=True,
    )


async def _ambient_status_handler(bot, interaction: discord.Interaction) -> None:
    if not await reject_if_not_mod(interaction):
        return
    sim = bot.ambient_sim
    state = "Running" if sim.is_running else "Stopped"
    lines = [f"**Ambient Sim** — {state}"]
    lines.append(f"Posts this session: `{sim.posts_since_start}`")
    if sim.last_post:
        key, channel_name, ts = sim.last_post
        ago = int(time.time() - ts)
        lines.append(f"Last post: `{key}` in `#{channel_name}` ({ago}s ago)")
    else:
        lines.append("Last post: —")
    lines.append(
        f"Corpus: `{sim._chain.corpus_size}` messages / `{sim._chain.vocab_size}` bigrams"
    )
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


def register(bot) -> None:
    guild_obj = discord.Object(id=bot.main_cfg.guild_id)

    @bot.tree.command(
        name="beta-ambient-start",
        description="Start the ambient puppet sim loop",
        guild=guild_obj,
    )
    async def start_cmd(interaction: discord.Interaction) -> None:
        await _ambient_start_handler(bot, interaction)

    @bot.tree.command(
        name="beta-ambient-stop",
        description="Stop the ambient puppet sim loop",
        guild=guild_obj,
    )
    async def stop_cmd(interaction: discord.Interaction) -> None:
        await _ambient_stop_handler(bot, interaction)

    @bot.tree.command(
        name="beta-ambient-status",
        description="Show ambient sim state, post count, and corpus info",
        guild=guild_obj,
    )
    async def status_cmd(interaction: discord.Interaction) -> None:
        await _ambient_status_handler(bot, interaction)
```

- [ ] **Step 4: Run tests — expect all pass**

```
pytest tests/beta/test_beta_slash_ambient.py -v
```

Expected: all 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add beta_tools/slash/ambient.py tests/beta/test_beta_slash_ambient.py
git commit -m "feat(ambient-sim): /beta-ambient-start/stop/status slash commands"
```

---

## Task 5: Wire Up Bot, Register Commands, Update Help

**Files:**
- Modify: `beta_tools/bot.py`
- Modify: `beta_tools/slash/__init__.py`
- Modify: `beta_tools/slash/help.py`

- [ ] **Step 1: Update `beta_tools/bot.py`**

Replace the full file with this (changes: import `Optional`, add `_chain` + `ambient_sim` attrs, load chain in `setup_hook`, instantiate sim + auto-start in `on_ready`, stop in `close`):

```python
# beta_tools/bot.py
"""DkToolsBot — the sidecar bot's commands.Bot subclass.

Owns the puppet manager, webhook fleet, ambient sim, and slash command tree.
Slash commands register in setup_hook() once the bot is logged in.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands

from beta_tools.config import BetaConfig
from beta_tools.puppet_manager import PuppetManager
from beta_tools.webhook_fleet import WebhookFleet
from config import Config

log = logging.getLogger("beta_tools.bot")


class DkToolsBot(commands.Bot):
    def __init__(self, *, main_cfg: Config, beta_cfg: BetaConfig) -> None:
        intents = discord.Intents.default()
        intents.message_content = False
        intents.members = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self.main_cfg = main_cfg
        self.beta_cfg = beta_cfg
        self.puppet_manager: Optional[PuppetManager] = None
        self.webhook_fleet: Optional[WebhookFleet] = None
        self.ambient_sim = None
        self._chain = None

    async def setup_hook(self) -> None:
        from beta_tools.markov import MarkovChain
        from beta_tools.personas import load_puppet_personas
        from beta_tools.slash import register_all

        # Load Markov chain if available
        chain_path = Path(__file__).resolve().parent.parent / "fixtures" / "markov_chain.json"
        if chain_path.exists():
            self._chain = MarkovChain.load(chain_path)
            log.info(
                "loaded Markov chain: %d bigrams, corpus=%d",
                self._chain.vocab_size,
                self._chain.corpus_size,
            )
        else:
            log.warning("fixtures/markov_chain.json not found — ambient sim will be unavailable")

        fixtures_dir = Path(__file__).resolve().parent.parent / "fixtures"
        personas = load_puppet_personas(fixtures_dir / "beta_puppets.yaml")
        pm = PuppetManager(
            personas=personas,
            tokens=self.beta_cfg.puppet_tokens,
            expected_ids=self.beta_cfg.puppet_expected_ids,
            expected_guild_id=self.main_cfg.guild_id,
        )
        self.puppet_manager = pm
        self.webhook_fleet = WebhookFleet()

        register_all(self)
        guild_obj = discord.Object(id=self.main_cfg.guild_id)
        await self.tree.sync(guild=guild_obj)
        log.info("registered /beta commands to guild %d", self.main_cfg.guild_id)

        log.info("starting %d puppets", len(pm.handles))
        await pm.start_all()
        await pm.apply_personas()
        log.info("puppets ready")

    async def on_ready(self) -> None:
        assert self.user is not None
        from beta_tools.ambient_sim import AmbientSim
        from beta_tools.safety import check_tools_bot_identity, check_tools_guild_membership

        check_tools_bot_identity(self.user, self.beta_cfg.tools_expected_id)
        await check_tools_guild_membership(self, self.main_cfg.guild_id)
        log.info(
            "DK Tools ready: %s (id=%d) in guild %d",
            self.user, self.user.id, self.main_cfg.guild_id,
        )

        if self._chain is not None and self.puppet_manager is not None:
            guild = self.get_guild(self.main_cfg.guild_id)
            if guild is not None:
                self.ambient_sim = AmbientSim(
                    chain=self._chain,
                    puppet_manager=self.puppet_manager,
                    guild=guild,
                    beta_cfg=self.beta_cfg,
                )
                if self.beta_cfg.ambient_autostart:
                    self.ambient_sim.start()
                    log.info("ambient sim auto-started")
            else:
                log.warning("guild not found in on_ready — ambient sim disabled")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if guild.id != self.main_cfg.guild_id:
            log.critical(
                "CRITICAL: DK Tools joined unexpected guild %d (%r) — leaving.",
                guild.id, guild.name,
            )
            try:
                await guild.leave()
            except Exception:  # noqa: BLE001
                log.exception("failed to leave guild %d", guild.id)

    async def close(self) -> None:
        if self.ambient_sim is not None:
            await self.ambient_sim.stop()
        if self.puppet_manager is not None:
            await self.puppet_manager.close_all()
        await super().close()
```

- [ ] **Step 2: Update `beta_tools/slash/__init__.py`**

```python
# beta_tools/slash/__init__.py
"""/beta slash commands.

register_all(bot) wires every slash module's commands onto bot.tree.
"""

from __future__ import annotations

from beta_tools.slash.ambient import register as register_ambient
from beta_tools.slash.help import register as register_help
from beta_tools.slash.puppets import register as register_puppets


def register_all(bot) -> None:
    register_help(bot)
    register_puppets(bot)
    register_ambient(bot)
```

- [ ] **Step 3: Update `beta_tools/slash/help.py`**

Add the "Ambient Sim" field. Replace the full file:

```python
# beta_tools/slash/help.py
"""/beta help — overview embed."""

from __future__ import annotations

import discord

from beta_tools.slash._base import reject_if_not_mod


def register(bot) -> None:
    guild_obj = discord.Object(id=bot.main_cfg.guild_id)

    @bot.tree.command(name="beta-help", description="Show DK Tools beta-mode commands", guild=guild_obj)
    async def beta_help(interaction: discord.Interaction) -> None:
        if not await reject_if_not_mod(interaction):
            return
        embed = discord.Embed(
            title="DK Tools — Beta Tester Commands",
            description="Slash commands available while running against the beta server.",
            color=discord.Color.dark_purple(),
        )
        embed.add_field(
            name="Puppets",
            value=(
                "`/beta-puppets-list` — show roster + connection state\n"
                "`/beta-puppets-reload` — re-read fixtures/beta_puppets.yaml\n"
                "`/beta-puppets-reconnect <key>` — reconnect a single puppet\n"
                "`/beta-puppets-impersonate <key> <channel> <text>` — drive a puppet manually\n"
                "`/beta-ghosts-impersonate <name> <avatar_url> <channel> <text>` — webhook ghost\n"
            ),
            inline=False,
        )
        embed.add_field(
            name="Ambient Sim",
            value=(
                "`/beta-ambient-start` — start the puppet chat simulation loop\n"
                "`/beta-ambient-stop` — stop the loop\n"
                "`/beta-ambient-status` — show state, post count, and corpus info\n"
            ),
            inline=False,
        )
        embed.set_footer(text="More commands ship in later beta_tools plans.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
```

- [ ] **Step 4: Update `tests/beta/test_beta_bot.py` — add ambient_sim attr check**

Add this test at the end of the file:

```python
def test_dk_tools_bot_has_ambient_sim_attr(mock_main_cfg, mock_beta_cfg):
    from beta_tools.bot import DkToolsBot
    bot = DkToolsBot(main_cfg=mock_main_cfg, beta_cfg=mock_beta_cfg)
    assert hasattr(bot, "ambient_sim")
    assert bot.ambient_sim is None  # set later in on_ready
```

- [ ] **Step 5: Run the full beta test suite**

```
pytest tests/beta/ -v
```

Expected: all tests pass, including the new `test_dk_tools_bot_has_ambient_sim_attr`.

- [ ] **Step 6: Commit**

```bash
git add beta_tools/bot.py beta_tools/slash/__init__.py beta_tools/slash/help.py tests/beta/test_beta_bot.py
git commit -m "feat(ambient-sim): wire AmbientSim into DkToolsBot — auto-start, stop on close, register commands"
```

---

## Task 6: Smoke Test End-to-End

- [ ] **Step 1: Restart the sidecar**

Stop the running sidecar (Ctrl+C), then:

```
python -m beta_tools
```

Expected log lines (in addition to existing startup):
```
INFO     beta_tools.bot loaded Markov chain: <N> bigrams, corpus=<M>
INFO     beta_tools.bot puppets ready
INFO     beta_tools.bot DK Tools ready: dktoools#1056 ...
INFO     beta_tools.ambient_sim ambient sim auto-started   ← if BETA_AMBIENT_AUTOSTART=1
INFO     beta_tools.ambient_sim ambient sim loop running
```

If `BETA_AMBIENT_AUTOSTART` is not set to `1` in `.env`, add it now and restart. Within ~15 seconds you should see:

```
DEBUG    beta_tools.ambient_sim ambient sim: 'alice' posted in #general
```

(Set log level to DEBUG in `.env` or check the Discord channel directly.)

- [ ] **Step 2: Verify messages appear in Discord**

Open the dev test guild and watch `#general`, `#random`, `#drama` — puppet messages should appear at roughly several per minute. The burst window means after the first post, the next few come quickly.

- [ ] **Step 3: Test slash commands in Discord**

In the test guild, run:
- `/beta-ambient-status` — should show running state, post count, corpus size
- `/beta-ambient-stop` — should stop the loop and report post count
- `/beta-ambient-start` — should restart it
- `/beta-help` — should show the new "Ambient Sim" section

- [ ] **Step 4: Run full test suite one final time**

```
pytest tests/beta/ -v -q
```

Expected: all tests green.
