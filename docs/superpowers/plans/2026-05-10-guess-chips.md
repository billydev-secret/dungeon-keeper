# Guess Round Message Chips Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two disabled chip buttons (`Guesses: N`, `Submitted by ▒▒▒▒▒▒▒`) to the public Guess round message in unsolved state, and bump the counter on every wrong guess.

**Architecture:** Extend `GameView` with a `guess_count` parameter and conditionally add two disabled chip buttons when `solved=False`. The chips have stable custom IDs but no callbacks (Discord still routes button interactions by custom_id, so the persistent `Guess` button keeps working across rebuilds). The `_game_embed` drops the "Submitted by an anonymous member" description. The wrong-guess path in `GuessSelectView._on_select` gains a counter-bump edit using a freshly-constructed `GameView`. Correct-and-solved and correct-but-lost-race paths skip the bump (the solved-embed swap or the now-solved view would be overwritten otherwise).

**Tech Stack:** Python, discord.py, pytest, sqlite (existing). No new deps.

**Spec:** `docs/superpowers/specs/2026-05-10-guess-chips-design.md`

---

## File Structure

| File | Role |
|------|------|
| `cogs/guess_cog.py` | All implementation changes — embed, `GameView`, `_on_select`, `cog_load`. |
| `tests/cogs/test_guess_guess.py` | Update wrong-guess test (now expects an edit); add tests for counter bump and skip cases. |
| `tests/cogs/test_guess_cog_load_limit.py` | Add a test that reconstructed views carry the correct count. |

No new files. No schema, migration, or config changes.

---

### Task 1: Extend `GameView` with chip rendering

**Files:**
- Modify: `cogs/guess_cog.py` (`GameView` class, `_game_embed`)
- Test: `tests/cogs/test_guess_cog_structure.py` (add a structural test in a fresh `class TestChipRendering`)

The `GameView` constructor gains an optional `guess_count: int = 0` keyword arg. When `solved=False`, it adds two disabled secondary chip buttons on a second action row. When `solved=True`, the view is unchanged (single `Guess late` button). `_game_embed` drops its description (image + title remain).

- [ ] **Step 1: Write the failing tests**

Append to `tests/cogs/test_guess_cog_structure.py`:

```python
import discord

from cogs.guess_cog import GameView, _game_embed


def test_unsolved_game_view_has_guess_button_and_two_chips():
    bot = MagicMock()
    view = GameView(bot, round_id=42, guess_count=7)
    children = view.children
    # Three components total: Guess + 2 chips
    assert len(children) == 3
    labels = [c.label for c in children]
    assert "Guess" in labels
    assert "Guesses: 7" in labels
    assert "Submitted by ▒▒▒▒▒▒▒" in labels
    # The two chips are disabled secondary buttons.
    chip_buttons = [c for c in children if c.label.startswith(("Guesses:", "Submitted by"))]
    for chip in chip_buttons:
        assert chip.disabled is True
        assert chip.style is discord.ButtonStyle.secondary
        assert chip.row == 1  # second action row


def test_unsolved_game_view_chip_custom_ids_are_round_scoped():
    bot = MagicMock()
    view = GameView(bot, round_id=99, guess_count=0)
    ids = {c.custom_id for c in view.children if c.custom_id}
    assert "guess_chip_count:99" in ids
    assert "guess_chip_submitter:99" in ids
    assert "guess_guess:99" in ids


def test_solved_game_view_omits_chips():
    bot = MagicMock()
    view = GameView(bot, round_id=42, solved=True)
    labels = [c.label for c in view.children]
    assert labels == ["Guess late"]


def test_game_embed_has_no_anonymous_description():
    embed = _game_embed(42)
    assert embed.description in (None, "")
    assert embed.title == "Round #42"
```

If `tests/cogs/test_guess_cog_structure.py` doesn't already import `MagicMock`, add `from unittest.mock import MagicMock` near the top.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/cogs/test_guess_cog_structure.py -v -k "chip or no_anonymous"`
Expected: 4 failures — `Guesses:` chip not found, `_game_embed` still has description, etc.

- [ ] **Step 3: Edit `_game_embed` to drop the description**

Replace this in `cogs/guess_cog.py`:

```python
def _game_embed(round_id: int) -> discord.Embed:
    return discord.Embed(
        title=f"Round #{round_id}",
        description="Submitted by an anonymous member",
        color=discord.Color.from_rgb(80, 20, 100),
    )
```

With:

```python
def _game_embed(round_id: int) -> discord.Embed:
    return discord.Embed(
        title=f"Round #{round_id}",
        color=discord.Color.from_rgb(80, 20, 100),
    )
```

- [ ] **Step 4: Add chip rendering to `GameView`**

Replace the `GameView.__init__` in `cogs/guess_cog.py`:

```python
    def __init__(self, bot: "Bot", round_id: int, *, solved: bool = False) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.round_id = round_id

        btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="Guess late" if solved else "Guess",
            style=discord.ButtonStyle.primary,
            custom_id=f"guess_guess:{round_id}",
        )
        btn.callback = self._guess_callback
        self.add_item(btn)
```

With:

```python
    def __init__(
        self,
        bot: "Bot",
        round_id: int,
        *,
        solved: bool = False,
        guess_count: int = 0,
    ) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.round_id = round_id
        self.guess_count = guess_count

        btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="Guess late" if solved else "Guess",
            style=discord.ButtonStyle.primary,
            custom_id=f"guess_guess:{round_id}",
            row=0,
        )
        btn.callback = self._guess_callback
        self.add_item(btn)

        if not solved:
            count_chip: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
                label=f"Guesses: {guess_count}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"guess_chip_count:{round_id}",
                disabled=True,
                row=1,
            )
            submitter_chip: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
                label="Submitted by ▒▒▒▒▒▒▒",
                style=discord.ButtonStyle.secondary,
                custom_id=f"guess_chip_submitter:{round_id}",
                disabled=True,
                row=1,
            )
            self.add_item(count_chip)
            self.add_item(submitter_chip)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/cogs/test_guess_cog_structure.py -v -k "chip or no_anonymous"`
Expected: 4 passes.

- [ ] **Step 6: Run the full guess test suite to confirm nothing else regressed yet**

Run: `pytest tests/cogs/test_guess_cog_structure.py tests/cogs/test_guess_guess.py tests/cogs/test_guess_submit.py tests/cogs/test_guess_cog_load_limit.py -v`
Expected: most pass; `test_wrong_guess_sends_not_it_message` likely still passes (we haven't added the bump yet). If anything else fails, stop and debug — we shouldn't have broken anything else with just an embed-description and chip-rendering change.

- [ ] **Step 7: Commit**

```bash
git add cogs/guess_cog.py tests/cogs/test_guess_cog_structure.py
git commit -m "feat(guess): render chip buttons on unsolved round view"
```

---

### Task 2: Bump the counter on every wrong guess (skip when round is or becomes solved)

**Files:**
- Modify: `cogs/guess_cog.py` (`GuessSelectView._on_select`)
- Test: `tests/cogs/test_guess_guess.py`

The wrong-guess branch in `_on_select` rebuilds a fresh `GameView` with the new count and edits the public message. Two conditions skip the bump:
1. The guess was correct — the win path replaces the entire view with the solved embed, and the lost-race path leaves the now-solved view in place.
2. `round_row.solved_at is not None` at load time — the public message already has the solved view; bumping would overwrite it with chips.

We compute the new count via `_do_count_guesses_for_round` after `_do_insert_guess`, so the value is authoritative even if other guesses interleaved.

- [ ] **Step 1: Update the failing wrong-guess test in `tests/cogs/test_guess_guess.py`**

Replace:

```python
@pytest.mark.asyncio
async def test_wrong_guess_sends_not_it_message():
    game_msg = MagicMock()
    game_msg.edit = AsyncMock()
    game_msg.guild = MagicMock()
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round()

    with patch("cogs.guess_cog._do_load_round", return_value=round_row), \
         patch("cogs.guess_cog._do_insert_guess"), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(3333)])):
        await view._on_select(interaction)

    game_msg.edit.assert_not_called()
    call_content = interaction.edit_original_response.call_args.kwargs.get("content", "")
    assert "not it" in call_content.lower()
```

With:

```python
@pytest.mark.asyncio
async def test_wrong_guess_bumps_counter_and_sends_not_it_message():
    game_msg = MagicMock()
    game_msg.edit = AsyncMock()
    game_msg.guild = MagicMock()
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round()

    with patch("cogs.guess_cog._do_load_round", return_value=round_row), \
         patch("cogs.guess_cog._do_insert_guess"), \
         patch("cogs.guess_cog._do_count_guesses_for_round", return_value=4), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(3333)])):
        await view._on_select(interaction)

    # Counter-bump edit: a fresh GameView with the new count is attached.
    game_msg.edit.assert_called_once()
    edit_kwargs = game_msg.edit.call_args.kwargs
    new_view = edit_kwargs["view"]
    labels = [c.label for c in new_view.children]
    assert "Guesses: 4" in labels

    call_content = interaction.edit_original_response.call_args.kwargs.get("content", "")
    assert "not it" in call_content.lower()
```

- [ ] **Step 2: Add a test for wrong-guess on already-solved round (no bump)**

Append to `tests/cogs/test_guess_guess.py`:

```python
@pytest.mark.asyncio
async def test_wrong_guess_on_solved_round_skips_counter_bump():
    """If the round was already solved when we loaded it, the public message
    already has the solved view — a chip-bump would overwrite it."""
    game_msg = MagicMock()
    game_msg.edit = AsyncMock()
    game_msg.guild = MagicMock()
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round(solved_at=1234.0)

    with patch("cogs.guess_cog._do_load_round", return_value=round_row), \
         patch("cogs.guess_cog._do_insert_guess"), \
         patch("cogs.guess_cog._do_count_guesses_for_round", return_value=8), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(3333)])):
        await view._on_select(interaction)

    game_msg.edit.assert_not_called()
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `pytest tests/cogs/test_guess_guess.py::test_wrong_guess_bumps_counter_and_sends_not_it_message tests/cogs/test_guess_guess.py::test_wrong_guess_on_solved_round_skips_counter_bump -v`
Expected: first test fails ("Expected `edit` to have been called once. Called 0 times."); second test passes (no bump logic yet, so no edit, which matches).

- [ ] **Step 4: Implement the counter bump in `_on_select`**

In `cogs/guess_cog.py`, find the final `else:` branch of `_on_select` (the wrong-guess path):

```python
        else:
            await interaction.edit_original_response(
                content="❌ Not it. Keep trying!",
                view=self,
            )
```

Replace it with:

```python
        else:
            if round_row.solved_at is None:
                new_count = await asyncio.to_thread(
                    _do_count_guesses_for_round, db_path, self.round_id
                )
                new_view = GameView(
                    self.bot, self.round_id, solved=False, guess_count=new_count
                )
                try:
                    await self.game_message.edit(view=new_view)
                except discord.HTTPException:
                    log.exception(
                        "guess: chip counter bump failed for round %d", self.round_id
                    )
            await interaction.edit_original_response(
                content="❌ Not it. Keep trying!",
                view=self,
            )
```

- [ ] **Step 5: Run the wrong-guess tests to verify they pass**

Run: `pytest tests/cogs/test_guess_guess.py::test_wrong_guess_bumps_counter_and_sends_not_it_message tests/cogs/test_guess_guess.py::test_wrong_guess_on_solved_round_skips_counter_bump -v`
Expected: both pass.

- [ ] **Step 6: Run the full guess test suite — confirm no regressions**

Run: `pytest tests/cogs/test_guess_guess.py tests/cogs/test_guess_submit.py tests/cogs/test_guess_cog_structure.py tests/cogs/test_guess_cog_load_limit.py -v`
Expected: all pass. The existing tests for correct-guess-wins-race, correct-guess-loses-race, and correct-on-solved-round paths shouldn't have changed behavior — they don't hit the wrong-guess branch.

- [ ] **Step 7: Commit**

```bash
git add cogs/guess_cog.py tests/cogs/test_guess_guess.py
git commit -m "feat(guess): bump guess counter chip on wrong guesses"
```

---

### Task 3: Reconstruct views with the current count at `cog_load`

**Files:**
- Modify: `cogs/guess_cog.py` (`GuessCog.cog_load`)
- Test: `tests/cogs/test_guess_cog_load_limit.py`

After a bot restart, persistent views are rebuilt from `get_unsolved_round_ids`. Today they all render with `guess_count=0` (effectively zero state). Fix: fetch the count per round and pass it to `GameView`.

- [ ] **Step 1: Write the failing test**

Append to `tests/cogs/test_guess_cog_load_limit.py`:

```python
@pytest.mark.asyncio
async def test_cog_load_passes_current_guess_count_to_view(sync_db_path: Path):
    """Reconstructed GameViews must carry the round's current guess count,
    so the chip label reflects reality after a restart."""
    from services.guess_repo import insert_guess

    with open_db(sync_db_path) as conn:
        rid = insert_round(conn, guild_id=1, submitter_id=10, answer_id=10)
        for i in range(3):
            insert_guess(conn, round_id=rid, guesser_id=100 + i,
                         guessed_user_id=999, correct=False)

    cog, add_view = _make_cog(sync_db_path)
    await cog.cog_load()  # type: ignore[attr-defined]

    assert add_view.call_count == 1
    reconstructed_view = add_view.call_args_list[0].args[0]
    labels = [c.label for c in reconstructed_view.children]
    assert "Guesses: 3" in labels
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/cogs/test_guess_cog_load_limit.py::test_cog_load_passes_current_guess_count_to_view -v`
Expected: FAIL — `"Guesses: 0"` is in labels, `"Guesses: 3"` is not.

- [ ] **Step 3: Update `cog_load` to fetch counts**

In `cogs/guess_cog.py`, replace `GuessCog.cog_load`:

```python
    async def cog_load(self) -> None:
        """Re-register persistent GameViews for unsolved rounds (capped)."""
        db_path = self.bot.ctx.db_path
        round_ids = await asyncio.to_thread(
            _do_load_unsolved_round_ids, db_path, limit=_COG_LOAD_VIEW_CAP
        )
        for rid in round_ids:
            self.bot.add_view(GameView(self.bot, rid, solved=False))
        log.info("guess: re-registered %d persistent GameViews (cap %d)",
                 len(round_ids), _COG_LOAD_VIEW_CAP)
```

With:

```python
    async def cog_load(self) -> None:
        """Re-register persistent GameViews for unsolved rounds (capped)."""
        db_path = self.bot.ctx.db_path
        round_ids = await asyncio.to_thread(
            _do_load_unsolved_round_ids, db_path, limit=_COG_LOAD_VIEW_CAP
        )
        for rid in round_ids:
            count = await asyncio.to_thread(
                _do_count_guesses_for_round, db_path, rid
            )
            self.bot.add_view(
                GameView(self.bot, rid, solved=False, guess_count=count)
            )
        log.info("guess: re-registered %d persistent GameViews (cap %d)",
                 len(round_ids), _COG_LOAD_VIEW_CAP)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/cogs/test_guess_cog_load_limit.py -v`
Expected: all pass (including the existing two).

- [ ] **Step 5: Run the full guess suite once more**

Run: `pytest tests/cogs/test_guess_cog_structure.py tests/cogs/test_guess_guess.py tests/cogs/test_guess_submit.py tests/cogs/test_guess_cog_load_limit.py tests/cogs/test_guess_setup.py tests/cogs/test_guess_delete.py tests/cogs/test_guess_round_inspector.py tests/cogs/test_guess_optout_listener.py -v`
Expected: all pass.

- [ ] **Step 6: Run ruff and pyright**

Run: `ruff check cogs/guess_cog.py tests/cogs/`
Expected: no errors.

Run: `pyright cogs/guess_cog.py`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add cogs/guess_cog.py tests/cogs/test_guess_cog_load_limit.py
git commit -m "feat(guess): restore current guess count on view reconstruction"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Implemented in |
|---|---|
| Drop "Submitted by an anonymous member" embed text | Task 1 (`_game_embed` edit) |
| Add `Guesses: N` chip on unsolved view | Task 1 (`GameView.__init__`) |
| Add `Submitted by ▒▒▒▒▒▒▒` chip on unsolved view | Task 1 (`GameView.__init__`) |
| Disabled chips, U+2592 redaction bar, custom IDs `guess_chip_count:{id}` / `guess_chip_submitter:{id}` | Task 1 (visible in code blocks) |
| Counter bumps on every wrong guess | Task 2 |
| Counter does NOT bump on correct-and-solved (the win path replaces the view) | Task 2 (no code change to win path; covered by existing test in step 6) |
| Counter does NOT bump on correct-but-lost-race (the now-solved view stays) | Task 2 (no code change to lost-race path; covered by existing test in step 6) |
| Counter does NOT bump on wrong guess against an already-solved round | Task 2 (`round_row.solved_at is None` guard) |
| Counter survives restart | Task 3 |
| Edit failure tolerance (log + swallow) | Task 2 (`try/except discord.HTTPException`) |
| Solved-state view unchanged (single `Guess late` button) | Task 1 (`if not solved:` guard) |

All spec sections accounted for. No placeholder steps. Type signature `guess_count: int = 0` is consistent across `GameView.__init__`, `cog_load`'s constructor call, and `_on_select`'s constructor call. Custom IDs `guess_chip_count:{round_id}` / `guess_chip_submitter:{round_id}` are used identically in code and tests.
