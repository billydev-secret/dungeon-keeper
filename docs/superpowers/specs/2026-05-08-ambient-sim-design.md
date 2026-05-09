# Ambient Sim — Design Spec

**Date:** 2026-05-08
**Status:** Approved
**Scope:** `beta_tools` sidecar — general chat simulation via puppet bots

---

## 1. Goal

Make the three puppet bots (alice, bob, clara) post realistic chat messages automatically in the dev test guild, at a fast enough rate to exercise XP gain, activity tracking, and message velocity features without manual intervention.

---

## 2. Constraints & Non-Goals

- **General chat only** for this phase — no Veil submissions, no confessions, no slash command interactions
- **No LLM at this stage** — `BETA_LLM_BLEND` flag reserved for a future phase
- **No DB writes from the sim loop** — the main bot's `on_message` handler picks up puppet messages naturally; no synthetic `source` tagging needed yet
- **Dev-only** — all ambient sim code runs inside the existing `beta_tools` sidecar, which already refuses to start outside `BOT_ENV=dev`

---

## 3. Architecture

### 3.1 New Files

| Path | Purpose |
|------|---------|
| `scripts/build_markov.py` | One-shot script: reads any `messages` DB, writes `fixtures/markov_chain.json` |
| `beta_tools/markov.py` | `MarkovChain` class: load from JSON, generate text |
| `beta_tools/ambient_sim.py` | `AmbientSim` class: dispatcher loop |
| `beta_tools/slash/ambient.py` | `/beta-ambient-start`, `/beta-ambient-stop`, `/beta-ambient-status` |

### 3.2 Modified Files

| Path | Change |
|------|--------|
| `beta_tools/bot.py` | Instantiate `AmbientSim`; auto-start if `BETA_AMBIENT_AUTOSTART=1` |
| `beta_tools/slash/__init__.py` | Register ambient commands |
| `beta_tools/slash/help.py` | Add "Ambient Sim" section |

### 3.3 Data Flow

**One-time corpus build:**
```
prod DB / dev DB
    └─ scripts/build_markov.py --db <path> --out fixtures/markov_chain.json
           └─ fixtures/markov_chain.json  (committed to repo)
```

**Sidecar startup:**
```
DkToolsBot.setup_hook()
    ├─ MarkovChain.load("fixtures/markov_chain.json")
    └─ AmbientSim(chain, puppet_manager, guild, beta_cfg)
           └─ .start()  ← if BETA_AMBIENT_AUTOSTART=1
```

**Each tick:**
```
AmbientSim._loop()
    ├─ pick puppet   (weighted by activity_weight)
    ├─ pick channel  (weighted by channel_affinities, resolved by name in guild)
    ├─ generate text (MarkovChain, trimmed by message_length_bias)
    ├─ puppet_client.get_channel(id).send(text)
    └─ last_post_at = now  → activates burst window
```

---

## 4. Markov Chain

### 4.1 Corpus Build (`scripts/build_markov.py`)

**Input:** any SQLite DB with `messages` and `known_users` tables (same schema as `dk_dev.db`).

**Filters applied:**
- `content IS NOT NULL`
- Content has ≥ 3 words after whitespace-split
- `known_users.is_bot = 0` (exclude bot messages)
- `messages.source IS NULL` (exclude previously synthetic data)

**Model:** bigram (order-2). State = `(word_n, word_n+1)`, transitions = list of words observed to follow that pair in the corpus.

**Output format** (`fixtures/markov_chain.json`):
```json
{
  "version": 1,
  "corpus_size": 12483,
  "chain": {
    "hello there": ["friend", "world", "everyone"],
    "there friend": ["!"]
  }
}
```

Keys are space-joined bigrams. Values are lists of observed following words (with repetition, so frequent continuations appear more often).

**Fallback:** if corpus has fewer than 100 messages after filtering, the script exits with an error rather than producing a low-quality chain. Point it at a richer DB.

### 4.2 Generation (`beta_tools/markov.py`)

```python
class MarkovChain:
    @classmethod
    def load(cls, path: Path) -> "MarkovChain": ...
    def generate(self, length_bias: str) -> str: ...
```

**`generate()` algorithm:**
1. Pick a random starting bigram from the chain keys
2. Walk: look up current bigram → pick a random follower → advance state
3. Stop when: punctuation token ends a sentence, OR word count exceeds length budget
4. If chain dead-ends (no transitions), restart from a random bigram and continue
5. Return joined string

**Length budgets:**

| `message_length_bias` | Word range |
|----------------------|-----------|
| `short` | 5–15 |
| `medium` | 10–30 |
| `long` | 20–60 |

---

## 5. Dispatcher Loop

### 5.1 `AmbientSim` class

```python
class AmbientSim:
    def start(self) -> None      # spawns asyncio task
    def stop(self) -> None       # cancels task, awaits clean exit
    @property
    def is_running(self) -> bool
    @property
    def posts_since_start(self) -> int
    @property
    def last_post(self) -> tuple[str, str, float] | None  # (puppet_key, channel_name, timestamp)
```

### 5.2 Tick Logic

```
base_interval  = 15s / BETA_AMBIENT_RATE_MULTIPLIER
burst_interval = 5s
burst_duration = 30s

each tick:
  sleep_for = burst_interval if in_burst_window else base_interval ± 20% jitter
  await asyncio.sleep(sleep_for)

  puppet  = weighted_choice(handles, weights=[p.activity_weight for p in personas])
  channel = weighted_choice(channel_affinities) → resolve name in guild
  text    = chain.generate(puppet.persona.message_length_bias)

  await puppet.client.get_channel(channel_id).send(text)

  last_post_at = now
  posts_since_start += 1
```

**Rate at 1× multiplier:** ~4 posts/min base. During burst: ~12 posts/min for 30 seconds after each post.

**Jitter:** `base_interval * random.uniform(0.8, 1.2)` — prevents metronomic cadence.

### 5.3 Channel Resolution

- Channel affinities use names (`"general"`, `"random"`, etc.), not IDs
- At each pick, resolve name → `discord.TextChannel` by scanning `guild.text_channels`
- If a name doesn't resolve (channel doesn't exist in test guild): skip that option silently, log once at WARNING level, do not spam
- If no channels resolve at all for a puppet: skip that puppet's turn and try again next tick

### 5.4 Error Handling

- `discord.Forbidden` (no send perms): log WARNING, skip channel, continue loop
- `discord.HTTPException` (rate limit, transient): log WARNING, sleep 10s extra, continue
- Unhandled exception in loop body: log ERROR with traceback, sleep 30s, continue — the loop does not die on a single bad tick
- `asyncio.CancelledError`: caught at top of loop, exits cleanly (no suppression)

---

## 6. Slash Commands

All commands are guild-scoped to the test guild and gated by `reject_if_not_mod`.

### `/beta-ambient-start`
- If already running → ephemeral "Ambient sim is already running"
- Calls `sim.start()`
- Responds: "Ambient sim started — base interval `{base_interval:.0f}s`, burst `5s` for `30s` after each post"

### `/beta-ambient-stop`
- If not running → ephemeral "Ambient sim is not running"
- Calls `sim.stop()`
- Responds: "Ambient sim stopped — `{posts_since_start}` posts sent this session"

### `/beta-ambient-status`
- Always responds (running or not)
- Fields: running state, posts since start, last puppet + channel + time-ago, current interval mode (burst/base), corpus size (word count)

### `/beta-help` update
Add "Ambient Sim" field listing the three commands.

---

## 7. Configuration

All existing env vars, no new ones needed:

| Var | Effect |
|-----|--------|
| `BETA_AMBIENT_AUTOSTART=1` | `start()` called automatically in `setup_hook()` |
| `BETA_AMBIENT_RATE_MULTIPLIER=2.0` | Halves base interval (doubles posting rate) |

---

## 8. Corpus File

`fixtures/markov_chain.json` is committed to the repo. It contains no user identifiers — only word transition data. Regenerate by running:

```
python scripts/build_markov.py --db path/to/dk.db --out fixtures/markov_chain.json
```

Point at prod DB for the richest corpus. The file is safe to commit (no PII, no message IDs, no author data).

---

## 9. Out of Scope (Future Phases)

- LLM-generated messages (`BETA_LLM_BLEND=1`)
- Veil submissions and guessing
- Voice channel joins/leaves (`voice_likely` field reserved for later)
- `source='beta_sim'` tagging on generated messages
- Per-channel Markov chains
- Puppet reply threading (quoting/replying to specific messages)
