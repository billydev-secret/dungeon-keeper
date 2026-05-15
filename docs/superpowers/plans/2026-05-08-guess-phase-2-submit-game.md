# Guess Phase 2: Submit Command + Game Flow

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `/guess submit` so a Guess-roled member can post a cropped image as a guessing-game round, and any server member can guess the answer via a dropdown.

**Architecture:** All Discord interaction code lives in `cogs/guess_cog.py` (expanded from the Phase 1 stub) alongside the three View classes (SubmitPreviewView, GameView, GuessSelectView). The DB layer (`services/guess_repo.py`) and pipeline (`services/guess_pipeline.py`) are already built; this plan only wires them to Discord. GameView is registered as a persistent view so Guess buttons survive bot restarts.

**Tech Stack:** discord.py 2.x `app_commands.Group`, `discord.ui.View`, `asyncio.to_thread` for sync DB/pipeline calls, PIL for dimension check, `services/guess_pipeline.py`, `services/guess_repo.py`, `tests/fakes.py` + `@pytest.mark.asyncio` for cog tests.

---

## File Map

| File | Status | Purpose |
|------|--------|---------|
| `cogs/guess_cog.py` | Modify | Module-level helpers, all three Views, GuessCog with `/guess submit`, startup re-registration |
| `services/guess_repo.py` | Modify | Add `get_all_active_round_ids` for startup |
| `tests/unit/test_guess_helpers.py` | Create | Unit tests for pure validation helpers |
| `tests/unit/test_guess_repo.py` | Modify | Add test for `get_all_active_round_ids` |
| `tests/cogs/test_guess_submit.py` | Create | Cog-level tests for submit command |
| `tests/cogs/test_guess_guess.py` | Create | Cog-level tests for guess + solve flow |

---

## Context: What Phase 1 Built

Phase 1 already provides (do NOT re-implement these):
- **`services/guess_pipeline.py`**: `run_pipeline(image_path, image_bytes, difficulty, *, candidate_count, cache_dir) -> PipelineResult` and `run_reroll(image_bytes, existing_crops, *, jpeg_quality) -> bytes`
- **`services/guess_repo.py`**: `get_guess_config`, `insert_round`, `update_round_message`, `mark_round_solved`, `set_round_reroll_count`, `count_guesses_for_round`, `count_unique_guessers_for_round`, `insert_guess`, `get_round`
- **`services/guess_models.py`**: `GuessConfig`, `GuessRound`, `PipelineResult`, `BoundingBox`
- **`cogs/guess_cog.py`**: stub `GuessCog` with a placeholder `guess_status` command
- **`tests/fakes.py`**: `fake_interaction()`, `FakeGuild`, `FakeMember`, `FakeRole`, `FakeChannel`
- **`tests/conftest.py`**: `sync_db_path`, `fake_interaction` fixtures

The DB schema (`guess_rounds`, `guess_guesses`, `guess_optins`) is migrated and ready.

---

## Task 1: Validation helpers + unit tests

**Files:**
- Modify: `cogs/guess_cog.py` (add helpers above `GuessCog` class, replacing the stub)
- Create: `tests/unit/test_guess_helpers.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_guess_helpers.py`:

```python
"""Unit tests for pure validation helpers in guess_cog."""
from __future__ import annotations

import io
import pytest
from PIL import Image

from tests.fakes import FakeMember, FakeRole


def _make_jpeg(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(128, 0, 64)).save(buf, format="JPEG")
    return buf.getvalue()


class TestHasGuessRole:
    def test_member_with_role_returns_true(self):
        from cogs.guess_cog import _has_guess_role
        m = FakeMember(roles=[FakeRole(id=777)])
        assert _has_guess_role(m, 777) is True

    def test_member_without_role_returns_false(self):
        from cogs.guess_cog import _has_guess_role
        m = FakeMember(roles=[FakeRole(id=999)])
        assert _has_guess_role(m, 777) is False

    def test_member_with_no_roles_returns_false(self):
        from cogs.guess_cog import _has_guess_role
        m = FakeMember(roles=[])
        assert _has_guess_role(m, 777) is False


class TestValidateMime:
    def test_jpeg_accepted(self):
        from cogs.guess_cog import _validate_mime
        assert _validate_mime("image/jpeg") is True

    def test_png_accepted(self):
        from cogs.guess_cog import _validate_mime
        assert _validate_mime("image/png") is True

    def test_video_rejected(self):
        from cogs.guess_cog import _validate_mime
        assert _validate_mime("video/mp4") is False

    def test_none_rejected(self):
        from cogs.guess_cog import _validate_mime
        assert _validate_mime(None) is False


class TestValidateSize:
    def test_within_limit(self):
        from cogs.guess_cog import _validate_size
        assert _validate_size(5 * 1024 * 1024, max_mb=10) is True

    def test_at_limit(self):
        from cogs.guess_cog import _validate_size
        assert _validate_size(10 * 1024 * 1024, max_mb=10) is True

    def test_over_limit(self):
        from cogs.guess_cog import _validate_size
        assert _validate_size(10 * 1024 * 1024 + 1, max_mb=10) is False


class TestValidateDimensions:
    def test_large_enough_image(self):
        from cogs.guess_cog import _validate_dimensions
        ok, w, h = _validate_dimensions(_make_jpeg(500, 500), min_px=400)
        assert ok is True
        assert w == 500
        assert h == 500

    def test_too_narrow(self):
        from cogs.guess_cog import _validate_dimensions
        ok, w, h = _validate_dimensions(_make_jpeg(300, 600), min_px=400)
        assert ok is False

    def test_too_short(self):
        from cogs.guess_cog import _validate_dimensions
        ok, w, h = _validate_dimensions(_make_jpeg(600, 300), min_px=400)
        assert ok is False

    def test_exactly_at_limit(self):
        from cogs.guess_cog import _validate_dimensions
        ok, _, _ = _validate_dimensions(_make_jpeg(400, 400), min_px=400)
        assert ok is True
```

- [ ] **Step 2: Run tests to confirm they fail**

```
pytest tests/unit/test_guess_helpers.py -v
```
Expected: `ImportError` or `ModuleNotFoundError` — the helpers don't exist yet.

- [ ] **Step 3: Replace `cogs/guess_cog.py` with the implementation**

```python
"""Guess cog — NSFW guessing game (Phase 2)."""
from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from db_utils import open_db
from services.guess_models import GuessConfig, GuessRound
from services.guess_pipeline import run_pipeline
from services.guess_repo import (
    count_guesses_for_round,
    count_unique_guessers_for_round,
    get_all_active_round_ids,
    get_round,
    get_guess_config,
    insert_guess,
    insert_round,
    mark_round_solved,
    set_round_reroll_count,
    update_round_message,
)

if TYPE_CHECKING:
    from app_context import Bot

log = logging.getLogger("dungeonkeeper.guess")

_GUESS_CACHE = Path("guess_cache")


# ── Pure validation helpers (module-level so they're patchable in tests) ─────

def _has_guess_role(member: discord.Member, guess_role_id: int) -> bool:
    return any(r.id == guess_role_id for r in member.roles)


def _validate_mime(content_type: str | None) -> bool:
    return content_type is not None and content_type.startswith("image/")


def _validate_size(byte_count: int, max_mb: int) -> bool:
    return byte_count <= max_mb * 1024 * 1024


def _validate_dimensions(image_bytes: bytes, min_px: int) -> tuple[bool, int, int]:
    from PIL import Image  # noqa: PLC0415
    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size
    return (min(w, h) >= min_px, w, h)


# ── DB helpers (sync, called via asyncio.to_thread) ──────────────────────────

def _load_config(db_path: Path, guild_id: int) -> GuessConfig:
    with open_db(db_path) as conn:
        return get_guess_config(conn, guild_id)


def _do_insert_round(
    db_path: Path,
    *,
    guild_id: int,
    submitter_id: int,
    answer_id: int,
    channel_id: int,
    difficulty: str,
    allow_reuse: bool,
    candidate_count: int,
) -> int:
    with open_db(db_path) as conn:
        return insert_round(
            conn,
            guild_id=guild_id,
            submitter_id=submitter_id,
            answer_id=answer_id,
            channel_id=channel_id,
            difficulty=difficulty,
            allow_reuse=allow_reuse,
            candidate_count=candidate_count,
        )


def _do_update_round_message(
    db_path: Path, round_id: int, message_id: int, crop_url: str, crop_path: str
) -> None:
    with open_db(db_path) as conn:
        update_round_message(conn, round_id, message_id=message_id, crop_url=crop_url, crop_path=crop_path)


def _do_set_reroll_count(db_path: Path, round_id: int, count: int) -> None:
    with open_db(db_path) as conn:
        set_round_reroll_count(conn, round_id, count)


def _do_load_round(db_path: Path, round_id: int) -> GuessRound | None:
    with open_db(db_path) as conn:
        return get_round(conn, round_id)


def _do_insert_guess(
    db_path: Path,
    *,
    round_id: int,
    guesser_id: int,
    guessed_user_id: int,
    correct: bool,
) -> None:
    with open_db(db_path) as conn:
        insert_guess(conn, round_id=round_id, guesser_id=guesser_id,
                     guessed_user_id=guessed_user_id, correct=correct)


def _do_mark_solved(
    db_path: Path, round_id: int, *, solver_id: int
) -> tuple[int, int]:
    with open_db(db_path) as conn:
        guess_count = count_guesses_for_round(conn, round_id)
        unique_count = count_unique_guessers_for_round(conn, round_id)
        mark_round_solved(
            conn, round_id,
            solver_id=solver_id,
            guesses_to_solve=guess_count,
            unique_guessers_to_solve=unique_count,
        )
    return guess_count, unique_count


def _do_load_active_rounds(db_path: Path) -> list[tuple[int, bool]]:
    with open_db(db_path) as conn:
        return get_all_active_round_ids(conn)


# ── Embed helpers ─────────────────────────────────────────────────────────────

def _game_embed(round_id: int) -> discord.Embed:
    return discord.Embed(
        title=f"Round #{round_id}",
        description="Submitted by an anonymous member",
        color=discord.Color.from_rgb(80, 20, 100),
    ).set_image(url="attachment://guess_crop.jpg")


def _solved_embed(
    round_id: int,
    answer_mention: str,
    submitter_mention: str,
    solver_mention: str,
    guess_count: int,
    unique_count: int,
) -> discord.Embed:
    guesses_txt = f"{guess_count} guess{'es' if guess_count != 1 else ''}"
    guessers_txt = f"{unique_count} guesser{'s' if unique_count != 1 else ''}"
    return discord.Embed(
        title=f"✅ Round #{round_id} — Solved!",
        color=discord.Color.green(),
        description=(
            f"**Answer:** {answer_mention}\n"
            f"**Submitted by:** {submitter_mention}\n"
            f"**Solved by:** {solver_mention} in {guesses_txt} (across {guessers_txt})"
        ),
    )


# ── Views (defined before GuessCog so GuessCog can reference them) ──────────────

class GuessSelectView(discord.ui.View):
    """Ephemeral view shown when a user clicks the Guess button."""

    def __init__(
        self,
        bot: "Bot",
        round_id: int,
        guess_members: list[discord.Member],
        game_message: discord.Message,
    ) -> None:
        super().__init__(timeout=60)
        self.bot = bot
        self.round_id = round_id
        self.game_message = game_message

        options = [
            discord.SelectOption(label=m.display_name[:100], value=str(m.id))
            for m in guess_members[:25]
        ]
        self._select: discord.ui.Select = discord.ui.Select(  # type: ignore[type-arg]
            placeholder="Who is in this photo?",
            options=options,
            min_values=1,
            max_values=1,
        )
        self._select.callback = self._on_select
        self.add_item(self._select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        guessed_user_id = int(self._select.values[0])
        db_path = self.bot.ctx.db_path

        round_row = await asyncio.to_thread(_do_load_round, db_path, self.round_id)
        if round_row is None:
            await interaction.response.edit_message(content="Round not found.", view=None)
            return

        correct = guessed_user_id == round_row.answer_id
        await asyncio.to_thread(
            _do_insert_guess,
            db_path,
            round_id=self.round_id,
            guesser_id=interaction.user.id,
            guessed_user_id=guessed_user_id,
            correct=correct,
        )

        self._select.disabled = True

        if correct and round_row.solved_at is None:
            guess_count, unique_count = await asyncio.to_thread(
                _do_mark_solved, db_path, self.round_id, solver_id=interaction.user.id
            )
            answer_mention = f"<@{round_row.answer_id}>"
            submitter_mention = f"<@{round_row.submitter_id}>"
            solved_emb = _solved_embed(
                self.round_id, answer_mention, submitter_mention,
                interaction.user.mention, guess_count, unique_count,
            )
            new_game_view = GameView(self.bot, self.round_id, solved=True)
            await self.game_message.edit(embed=solved_emb, view=new_game_view)
            await interaction.response.edit_message(
                content=f"✅ **Correct!** You solved Round #{self.round_id}!",
                view=self,
            )
        elif correct:
            await interaction.response.edit_message(
                content="✅ Correct — but someone already solved this one.",
                view=self,
            )
        else:
            await interaction.response.edit_message(
                content="❌ Not it. Keep trying!",
                view=self,
            )


class GameView(discord.ui.View):
    """Persistent public view attached to a game message."""

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

    async def _guess_callback(self, interaction: discord.Interaction) -> None:
        db_path = self.bot.ctx.db_path
        config = await asyncio.to_thread(_load_config, db_path, interaction.guild_id)

        round_row = await asyncio.to_thread(_do_load_round, db_path, self.round_id)
        if round_row and interaction.user.id == round_row.submitter_id:
            await interaction.response.send_message(
                "You can't guess on your own round.", ephemeral=True
            )
            return

        guess_role = interaction.guild.get_role(config.guess_role_id)
        if guess_role is None:
            await interaction.response.send_message(
                "Guess role not found — ask an admin to configure it.", ephemeral=True
            )
            return

        guess_members = [m for m in guess_role.members if not m.bot]
        if not guess_members:
            await interaction.response.send_message(
                "No opted-in members to guess from.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Who do you think this is?",
            view=GuessSelectView(self.bot, self.round_id, guess_members, interaction.message),
            ephemeral=True,
        )


class SubmitPreviewView(discord.ui.View):
    """Ephemeral view shown to the submitter after pipeline runs."""

    _MAX_REROLLS = 3

    def __init__(
        self,
        bot: "Bot",
        round_id: int,
        crops: list[bytes],
        game_message: discord.Message,
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.round_id = round_id
        self.crops = crops
        self.crop_index = 0
        self.rerolls_used = 0
        self.game_message = game_message

        remaining = min(self._MAX_REROLLS, len(crops) - 1)
        self.reroll_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label=f"Re-roll ({remaining} left)",
            style=discord.ButtonStyle.secondary,
            disabled=remaining == 0,
        )
        self.reroll_btn.callback = self._on_reroll
        self.add_item(self.reroll_btn)

    async def _on_reroll(self, interaction: discord.Interaction) -> None:
        self.crop_index += 1
        self.rerolls_used += 1
        remaining = min(self._MAX_REROLLS - self.rerolls_used, len(self.crops) - 1 - self.crop_index)

        await asyncio.to_thread(
            _do_set_reroll_count, self.bot.ctx.db_path, self.round_id, self.rerolls_used
        )

        new_crop = self.crops[self.crop_index]

        # Update the public game message with new crop
        new_game_file = discord.File(io.BytesIO(new_crop), filename="guess_crop.jpg")
        await self.game_message.edit(
            embed=_game_embed(self.round_id),
            attachments=[new_game_file],
        )

        # Update ephemeral preview
        if remaining <= 0:
            self.reroll_btn.disabled = True
        else:
            self.reroll_btn.label = f"Re-roll ({remaining} left)"

        preview_file = discord.File(io.BytesIO(new_crop), filename="preview.jpg")
        preview_embed = discord.Embed(
            title="Your crop preview",
            description=f"Re-rolls remaining: {remaining}",
        ).set_image(url="attachment://preview.jpg")
        await interaction.response.edit_message(
            embed=preview_embed,
            attachments=[preview_file],
            view=self,
        )


# ── GuessCog ──────────────────────────────────────────────────────────────────

class GuessCog(commands.Cog):
    guess = app_commands.Group(name="guess", description="Guess guessing game commands.")

    def __init__(self, bot: "Bot") -> None:
        self.bot = bot
        super().__init__()

    async def cog_load(self) -> None:
        """Re-register persistent GameViews for all active (non-deleted) rounds."""
        db_path = self.bot.ctx.db_path
        round_ids = await asyncio.to_thread(_do_load_active_rounds, db_path)
        for rid, solved in round_ids:
            self.bot.add_view(GameView(self.bot, rid, solved=solved))
        log.info("guess: re-registered %d persistent GameViews", len(round_ids))

    @guess.command(name="submit", description="Submit an image to start a Guess round.")
    @app_commands.describe(
        image="The NSFW image to submit",
        allow_reuse="Let the bot recycle this crop in future quiet stretches (default: false)",
    )
    async def guess_submit(
        self,
        interaction: discord.Interaction,
        image: discord.Attachment,
        allow_reuse: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        db_path = self.bot.ctx.db_path
        config = await asyncio.to_thread(_load_config, db_path, interaction.guild_id)

        # Role check
        member = interaction.guild.get_member(interaction.user.id)
        if not member or not _has_guess_role(member, config.guess_role_id):
            await interaction.followup.send(
                "You need the Guess role to submit.", ephemeral=True
            )
            return

        # Channel configured?
        if config.guess_channel_id == 0:
            await interaction.followup.send(
                "Guess channel is not configured. Ask an admin to run `/guess setup`.", ephemeral=True
            )
            return

        # MIME check
        if not _validate_mime(image.content_type):
            await interaction.followup.send("Please submit an image file.", ephemeral=True)
            return

        # Size check
        if not _validate_size(image.size, config.max_image_size_mb):
            await interaction.followup.send(
                f"Image too large. Maximum is {config.max_image_size_mb} MB.", ephemeral=True
            )
            return

        # Download (in memory only; never persisted as the original)
        image_bytes = await image.read()

        # Dimension check (PIL in thread)
        dim_ok, _w, _h = await asyncio.to_thread(
            _validate_dimensions, image_bytes, config.min_image_dimension_px
        )
        if not dim_ok:
            await interaction.followup.send(
                f"Image too small. Minimum dimension is {config.min_image_dimension_px}px.", ephemeral=True
            )
            return

        # Write to temp file for NudeNet (cleaned up after pipeline)
        tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=".jpg")
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(image_bytes)

            pipeline_result = await asyncio.to_thread(
                run_pipeline,
                tmp_path,
                image_bytes,
                config.crop_difficulty,
                candidate_count=3,
            )
        finally:
            tmp_path.unlink(missing_ok=True)

        if not pipeline_result.candidates:
            await interaction.followup.send(
                "Couldn't find a viable crop region — try a different image.", ephemeral=True
            )
            return

        # Insert round (message_id / crop_url filled in after posting)
        round_id = await asyncio.to_thread(
            _do_insert_round,
            db_path,
            guild_id=interaction.guild_id,
            submitter_id=interaction.user.id,
            answer_id=interaction.user.id,
            channel_id=config.guess_channel_id,
            difficulty=config.crop_difficulty,
            allow_reuse=allow_reuse,
            candidate_count=len(pipeline_result.candidates),
        )

        # Write first crop to cache
        _GUESS_CACHE.mkdir(exist_ok=True)
        cache_path = _GUESS_CACHE / f"{round_id}.jpg"
        cache_path.write_bytes(pipeline_result.crops[0])

        # Post public game message
        guess_channel = interaction.guild.get_channel(config.guess_channel_id)
        if guess_channel is None:
            await interaction.followup.send(
                "Guess channel not found — ask an admin to check the config.", ephemeral=True
            )
            return

        crop_file = discord.File(io.BytesIO(pipeline_result.crops[0]), filename="guess_crop.jpg")
        game_view = GameView(self.bot, round_id)
        self.bot.add_view(game_view)
        game_msg = await guess_channel.send(
            embed=_game_embed(round_id), file=crop_file, view=game_view
        )

        # Update round with message info
        crop_url = game_msg.attachments[0].url if game_msg.attachments else ""
        await asyncio.to_thread(
            _do_update_round_message, db_path, round_id, game_msg.id, crop_url, str(cache_path)
        )

        # Send ephemeral preview to submitter
        preview_file = discord.File(io.BytesIO(pipeline_result.crops[0]), filename="preview.jpg")
        remaining_rerolls = min(SubmitPreviewView._MAX_REROLLS, len(pipeline_result.crops) - 1)
        preview_embed = discord.Embed(
            title="Your crop preview",
            description=f"Re-rolls remaining: {remaining_rerolls}",
        ).set_image(url="attachment://preview.jpg")
        await interaction.followup.send(
            embed=preview_embed,
            file=preview_file,
            view=SubmitPreviewView(self.bot, round_id, pipeline_result.crops, game_msg),
            ephemeral=True,
        )


async def setup(bot: "Bot") -> None:
    await bot.add_cog(GuessCog(bot))
```

- [ ] **Step 4: Run tests — all should pass**

```
pytest tests/unit/test_guess_helpers.py -v
```
Expected: 11 PASS.

- [ ] **Step 5: Commit**

```bash
git add cogs/guess_cog.py tests/unit/test_guess_helpers.py
git commit -m "feat(guess): validation helpers + command group scaffold"
```

---

## Task 2: `get_all_active_round_ids` in guess_repo + test

**Files:**
- Modify: `services/guess_repo.py` (add function at end)
- Modify: `tests/unit/test_guess_repo.py` (add two tests)

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_guess_repo.py`:

```python
from services.guess_repo import get_all_active_round_ids


def test_get_all_active_round_ids_returns_empty_when_no_rounds(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        result = get_all_active_round_ids(conn)
    assert result == []


def test_get_all_active_round_ids_returns_unsolved_and_solved(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        rid1 = insert_round(conn, guild_id=GUILD, submitter_id=USER_A, answer_id=USER_B)
        rid2 = insert_round(conn, guild_id=GUILD, submitter_id=USER_A, answer_id=USER_B)
        mark_round_solved(conn, rid2, solver_id=USER_B,
                          guesses_to_solve=1, unique_guessers_to_solve=1)
        result = get_all_active_round_ids(conn)
    ids_solved = {(rid, solved) for rid, solved in result}
    assert (rid1, False) in ids_solved
    assert (rid2, True) in ids_solved


def test_get_all_active_round_ids_excludes_deleted(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        rid = insert_round(conn, guild_id=GUILD, submitter_id=USER_A, answer_id=USER_B)
        soft_delete_round(conn, rid)
        result = get_all_active_round_ids(conn)
    assert result == []
```

Note: `mark_round_solved` and `soft_delete_round` are already imported in `test_guess_repo.py`; add them to the import line if not already present.

- [ ] **Step 2: Run to confirm failure**

```
pytest tests/unit/test_guess_repo.py::test_get_all_active_round_ids_returns_empty_when_no_rounds -v
```
Expected: `ImportError` — `get_all_active_round_ids` does not exist yet.

- [ ] **Step 3: Add function to `services/guess_repo.py`**

Add at the end of the file:

```python
def get_all_active_round_ids(conn: sqlite3.Connection) -> list[tuple[int, bool]]:
    rows = conn.execute(
        "SELECT id, solved_at IS NOT NULL AS solved FROM guess_rounds WHERE deleted_at IS NULL"
    ).fetchall()
    return [(row["id"], bool(row["solved"])) for row in rows]
```

- [ ] **Step 4: Run tests — all pass**

```
pytest tests/unit/test_guess_repo.py -v
```
Expected: all PASS (previously 21, now 24).

- [ ] **Step 5: Commit**

```bash
git add services/guess_repo.py tests/unit/test_guess_repo.py
git commit -m "feat(guess): add get_all_active_round_ids for startup re-registration"
```

---

## Task 3: Submit command — validation rejection paths + cog test scaffold

**Files:**
- Create: `tests/cogs/test_guess_submit.py`

Note: `tests/cogs/` already exists. No `__init__.py` needed (and must NOT be added — it breaks pytest import).

- [ ] **Step 1: Write failing tests**

Create `tests/cogs/test_guess_submit.py`:

```python
"""Cog-level tests for the /guess submit command."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.guess_models import GuessConfig
from tests.fakes import FakeGuild, FakeMember, FakeRole, fake_interaction

GUESS_ROLE_ID = 7001
GUESS_CHANNEL_ID = 8001
GUILD_ID = 9001


def _cfg(**overrides) -> GuessConfig:
    defaults = dict(
        guild_id=GUILD_ID,
        guess_role_id=GUESS_ROLE_ID,
        guess_channel_id=GUESS_CHANNEL_ID,
        guess_cooldown_seconds=30,
        crop_difficulty="medium",
        min_image_dimension_px=400,
        max_image_size_mb=10,
        reuse_enabled=True,
        reuse_quiet_hours=24,
        reuse_min_age_days=30,
        reuse_min_post_interval_hours=48,
    )
    defaults.update(overrides)
    return GuessConfig(**defaults)


def _guess_member(has_role: bool = True) -> FakeMember:
    roles = [FakeRole(id=GUESS_ROLE_ID)] if has_role else []
    return FakeMember(id=1001, roles=roles)


def _guild(member: FakeMember | None = None) -> FakeGuild:
    m = member or _guess_member()
    g = FakeGuild(id=GUILD_ID)
    g.members[m.id] = m
    return g


def _attachment(
    content_type: str = "image/jpeg",
    size: int = 1_000_000,
    read_return: bytes = b"fake-bytes",
) -> MagicMock:
    a = MagicMock()
    a.content_type = content_type
    a.size = size
    a.read = AsyncMock(return_value=read_return)
    return a


def _make_cog(db_path: str = ":memory:"):
    from cogs.guess_cog import GuessCog
    bot = MagicMock()
    bot.ctx.db_path = db_path
    bot.add_view = MagicMock()
    return GuessCog(bot)


# ── Validation rejection tests ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_rejects_no_guess_role():
    member = _guess_member(has_role=False)
    guild = _guild(member)
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)

    cog = _make_cog()
    with patch("cogs.guess_cog._load_config", return_value=_cfg()):
        await cog.guess_submit(interaction, image=_attachment(), allow_reuse=False)

    interaction.followup.send.assert_called_once()
    msg = interaction.followup.send.call_args.args[0]
    assert "Guess role" in msg


@pytest.mark.asyncio
async def test_submit_rejects_unconfigured_channel():
    member = _guess_member()
    guild = _guild(member)
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)

    cog = _make_cog()
    with patch("cogs.guess_cog._load_config", return_value=_cfg(guess_channel_id=0)):
        await cog.guess_submit(interaction, image=_attachment(), allow_reuse=False)

    msg = interaction.followup.send.call_args.args[0]
    assert "not configured" in msg.lower()


@pytest.mark.asyncio
async def test_submit_rejects_non_image_mime():
    member = _guess_member()
    guild = _guild(member)
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)

    cog = _make_cog()
    with patch("cogs.guess_cog._load_config", return_value=_cfg()):
        await cog.guess_submit(
            interaction, image=_attachment(content_type="video/mp4"), allow_reuse=False
        )

    msg = interaction.followup.send.call_args.args[0]
    assert "image" in msg.lower()


@pytest.mark.asyncio
async def test_submit_rejects_oversized_file():
    member = _guess_member()
    guild = _guild(member)
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)

    cog = _make_cog()
    with patch("cogs.guess_cog._load_config", return_value=_cfg(max_image_size_mb=5)):
        await cog.guess_submit(
            interaction,
            image=_attachment(size=6 * 1024 * 1024),
            allow_reuse=False,
        )

    msg = interaction.followup.send.call_args.args[0]
    assert "too large" in msg.lower() or "maximum" in msg.lower()


@pytest.mark.asyncio
async def test_submit_rejects_small_dimensions():
    member = _guess_member()
    guild = _guild(member)
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)

    cog = _make_cog()
    with patch("cogs.guess_cog._load_config", return_value=_cfg()):
        with patch("cogs.guess_cog._validate_dimensions", return_value=(False, 200, 200)):
            await cog.guess_submit(interaction, image=_attachment(), allow_reuse=False)

    msg = interaction.followup.send.call_args.args[0]
    assert "too small" in msg.lower() or "minimum" in msg.lower()


@pytest.mark.asyncio
async def test_submit_rejects_no_pipeline_candidates():
    import io as _io
    from PIL import Image
    from services.guess_models import PipelineResult

    buf = _io.BytesIO()
    Image.new("RGB", (500, 500)).save(buf, format="JPEG")
    img_bytes = buf.getvalue()

    member = _guess_member()
    guild = _guild(member)
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)

    cog = _make_cog()
    empty_result = PipelineResult(candidates=[], crops=[])

    with patch("cogs.guess_cog._load_config", return_value=_cfg()):
        with patch("cogs.guess_cog.run_pipeline", return_value=empty_result):
            await cog.guess_submit(
                interaction, image=_attachment(read_return=img_bytes), allow_reuse=False
            )

    msg = interaction.followup.send.call_args.args[0]
    assert "crop region" in msg.lower() or "viable" in msg.lower()
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/cogs/test_guess_submit.py -v
```
Expected: PASS for all tests (the command stub already defers + sends followup). Some may pass trivially because the command currently just sends "Phase 1 infrastructure ready" — re-run after Task 1 to confirm they pass against the real implementation.

Actually: after completing Task 1 (which replaced `guess_cog.py`), these tests should pass. Run now:

```
pytest tests/cogs/test_guess_submit.py -v
```
Expected: 6 PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/cogs/test_guess_submit.py
git commit -m "test(guess): submit command validation rejection tests"
```

---

## Task 4: Full submit flow integration test + pipeline wiring test

**Files:**
- Modify: `tests/cogs/test_guess_submit.py` (add success path test)

- [ ] **Step 1: Write failing test**

Add to `tests/cogs/test_guess_submit.py`:

```python
@pytest.mark.asyncio
async def test_submit_success_posts_game_and_sends_preview(sync_db_path: Path):
    """Full happy-path: pipeline returns a crop, game message posted, preview sent."""
    import io as _io
    from PIL import Image
    from services.guess_models import Detection, BoundingBox, PipelineResult

    buf = _io.BytesIO()
    Image.new("RGB", (500, 500)).save(buf, format="JPEG")
    img_bytes = buf.getvalue()

    det = Detection(label="BREAST", score=0.9, box=BoundingBox(10, 10, 100, 100))
    fake_result = PipelineResult(candidates=[det], crops=[b"fake-crop-jpeg"])

    member = _guess_member()
    guild = _guild(member)

    # Set up a fake guess channel on the guild
    fake_channel = MagicMock()
    fake_channel.send = AsyncMock(return_value=_fake_game_message())
    guild.channels[GUESS_CHANNEL_ID] = fake_channel

    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)
    interaction.guild.get_channel = lambda cid: guild.channels.get(cid)
    interaction.user.id = member.id

    cog = _make_cog(str(sync_db_path))
    with patch("cogs.guess_cog._load_config", return_value=_cfg()):
        with patch("cogs.guess_cog.run_pipeline", return_value=fake_result):
            with patch("cogs.guess_cog._do_insert_round", return_value=42):
                with patch("cogs.guess_cog._do_update_round_message"):
                    with patch("cogs.guess_cog._do_set_reroll_count"):
                        await cog.guess_submit(
                            interaction, image=_attachment(read_return=img_bytes), allow_reuse=False
                        )

    # Game message posted to the guess channel
    fake_channel.send.assert_called_once()

    # Ephemeral preview sent to submitter
    interaction.followup.send.assert_called_once()
    call_kwargs = interaction.followup.send.call_args
    assert call_kwargs.kwargs.get("ephemeral") is True


def _fake_game_message() -> MagicMock:
    """Minimal discord.Message fake for testing."""
    msg = MagicMock()
    msg.id = 12345
    msg.attachments = [MagicMock(url="https://cdn.discord.com/fake/crop.jpg")]
    msg.edit = AsyncMock()
    msg.guild = MagicMock()
    return msg
```

- [ ] **Step 2: Run to confirm**

```
pytest tests/cogs/test_guess_submit.py::test_submit_success_posts_game_and_sends_preview -v
```
Expected: PASS (after Task 1 implementation is in place).

- [ ] **Step 3: Run full unit + component + cog suite to confirm no regressions**

```
pytest tests/unit/ tests/components/ tests/cogs/ -q
```
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add tests/cogs/test_guess_submit.py
git commit -m "test(guess): submit success path integration test"
```

---

## Task 5: Guess flow — GuessSelectView + guess processing + solve logic

**Files:**
- Create: `tests/cogs/test_guess_guess.py`

The `GuessSelectView`, `GameView`, and solve logic were already implemented in Task 1. This task adds tests for them.

- [ ] **Step 1: Write failing tests**

Create `tests/cogs/test_guess_guess.py`:

```python
"""Cog-level tests for the Guess guess flow."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.guess_models import GuessRound
from tests.fakes import FakeGuild, FakeMember, FakeRole, fake_interaction

GUESS_ROLE_ID = 7001
ROUND_ID = 99


def _make_round(
    *,
    round_id: int = ROUND_ID,
    submitter_id: int = 1001,
    answer_id: int = 2001,
    solved_at: float | None = None,
) -> GuessRound:
    return GuessRound(
        id=round_id,
        guild_id=9001,
        submitter_id=submitter_id,
        answer_id=answer_id,
        channel_id=8001,
        message_id=12345,
        crop_path="/tmp/fake.jpg",
        crop_url="https://cdn.discord.com/fake.jpg",
        difficulty="medium",
        candidate_count=1,
        reroll_count=0,
        allow_reuse=False,
        is_reuse=False,
        original_round_id=None,
        reuse_blocked=False,
        created_at=1000.0,
        solved_at=solved_at,
        solver_id=None,
        guesses_to_solve=None,
        unique_guessers_to_solve=None,
        answer_optout=False,
        deleted_at=None,
    )


def _make_select_view(
    bot=None,
    guess_members: list | None = None,
    game_message=None,
    round_id: int = ROUND_ID,
):
    from cogs.guess_cog import GuessSelectView

    if bot is None:
        bot = MagicMock()
        bot.ctx.db_path = ":memory:"
    if guess_members is None:
        guess_members = [FakeMember(id=2001, display_name="Alice")]
    if game_message is None:
        game_message = MagicMock()
        game_message.edit = AsyncMock()
        game_message.guild = MagicMock()

    return GuessSelectView(bot, round_id, guess_members, game_message)


# ── GuessSelectView tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_correct_first_guess_marks_solved_and_edits_message():
    view = _make_select_view()
    view._select.values = [str(2001)]  # correct: answer_id = 2001
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.edit_message = AsyncMock()

    round_row = _make_round()  # not yet solved

    with patch("cogs.guess_cog._do_load_round", return_value=round_row):
        with patch("cogs.guess_cog._do_insert_guess"):
            with patch("cogs.guess_cog._do_mark_solved", return_value=(3, 2)):
                await view._on_select(interaction)

    # Game message should have been edited with solved embed
    view.game_message.edit.assert_called_once()

    # Ephemeral response says "Correct"
    call_content = interaction.response.edit_message.call_args.kwargs.get("content", "")
    assert "Correct" in call_content or "correct" in call_content.lower()


@pytest.mark.asyncio
async def test_correct_guess_already_solved_does_not_edit_game_message():
    view = _make_select_view()
    view._select.values = [str(2001)]  # correct
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.edit_message = AsyncMock()

    # Round is already solved
    round_row = _make_round(solved_at=1234.0)

    with patch("cogs.guess_cog._do_load_round", return_value=round_row):
        with patch("cogs.guess_cog._do_insert_guess"):
            await view._on_select(interaction)

    view.game_message.edit.assert_not_called()
    call_content = interaction.response.edit_message.call_args.kwargs.get("content", "")
    assert "already" in call_content.lower() or "someone" in call_content.lower()


@pytest.mark.asyncio
async def test_wrong_guess_sends_not_it_message():
    view = _make_select_view()
    view._select.values = [str(3333)]  # wrong: answer_id = 2001
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.edit_message = AsyncMock()

    round_row = _make_round()

    with patch("cogs.guess_cog._do_load_round", return_value=round_row):
        with patch("cogs.guess_cog._do_insert_guess"):
            await view._on_select(interaction)

    view.game_message.edit.assert_not_called()
    call_content = interaction.response.edit_message.call_args.kwargs.get("content", "")
    assert "Not it" in call_content or "not it" in call_content.lower()


@pytest.mark.asyncio
async def test_select_is_disabled_after_guess():
    view = _make_select_view()
    view._select.values = [str(2001)]
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.edit_message = AsyncMock()

    round_row = _make_round()
    with patch("cogs.guess_cog._do_load_round", return_value=round_row):
        with patch("cogs.guess_cog._do_insert_guess"):
            with patch("cogs.guess_cog._do_mark_solved", return_value=(1, 1)):
                await view._on_select(interaction)

    assert view._select.disabled is True


# ── GameView tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_game_view_rejects_submitter_guessing_own_round():
    from cogs.guess_cog import GameView

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    view = GameView(bot, ROUND_ID)

    submitter = FakeMember(id=1001)
    interaction = fake_interaction(user=submitter)
    interaction.guild_id = 9001
    interaction.response.send_message = AsyncMock()

    round_row = _make_round(submitter_id=1001)
    with patch("cogs.guess_cog._load_config"):
        with patch("cogs.guess_cog._do_load_round", return_value=round_row):
            await view._guess_callback(interaction)

    msg = interaction.response.send_message.call_args.args[0]
    assert "can't guess" in msg.lower() or "own round" in msg.lower()


# ── SubmitPreviewView re-roll tests ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_reroll_cycles_to_next_crop_and_updates_game_message():
    from cogs.guess_cog import SubmitPreviewView

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    game_msg = MagicMock()
    game_msg.edit = AsyncMock()

    view = SubmitPreviewView(bot, ROUND_ID, [b"crop0", b"crop1", b"crop2"], game_msg)
    interaction = fake_interaction()
    interaction.response.edit_message = AsyncMock()

    with patch("cogs.guess_cog._do_set_reroll_count"):
        await view._on_reroll(interaction)

    assert view.crop_index == 1
    game_msg.edit.assert_called_once()
    interaction.response.edit_message.assert_called_once()


@pytest.mark.asyncio
async def test_reroll_button_disabled_after_max_rerolls():
    from cogs.guess_cog import SubmitPreviewView

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    game_msg = MagicMock()
    game_msg.edit = AsyncMock()

    # 4 crops available, MAX_REROLLS = 3
    view = SubmitPreviewView(bot, ROUND_ID, [b"c0", b"c1", b"c2", b"c3"], game_msg)

    interaction = fake_interaction()
    interaction.response.edit_message = AsyncMock()

    with patch("cogs.guess_cog._do_set_reroll_count"):
        # Exhaust all 3 re-rolls
        for _ in range(3):
            await view._on_reroll(interaction)

    assert view.reroll_btn.disabled is True


@pytest.mark.asyncio
async def test_reroll_button_disabled_when_only_one_crop():
    from cogs.guess_cog import SubmitPreviewView

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    game_msg = MagicMock()

    view = SubmitPreviewView(bot, ROUND_ID, [b"only-crop"], game_msg)

    assert view.reroll_btn.disabled is True
```

- [ ] **Step 2: Run to confirm they pass**

```
pytest tests/cogs/test_guess_guess.py -v
```
Expected: 9 PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/cogs/test_guess_guess.py
git commit -m "test(guess): guess flow, solve logic, reroll view tests"
```

---

## Task 6: Startup re-registration test + full suite check

**Files:**
- Modify: `tests/cogs/test_guess_submit.py` (add `cog_load` test)

- [ ] **Step 1: Write test**

Add to `tests/cogs/test_guess_submit.py`:

```python
@pytest.mark.asyncio
async def test_cog_load_registers_game_views_from_db(sync_db_path: Path):
    """cog_load queries active rounds and calls bot.add_view for each."""
    from services.guess_repo import insert_round
    from db_utils import open_db

    with open_db(sync_db_path) as conn:
        insert_round(conn, guild_id=GUILD_ID, submitter_id=1001, answer_id=1001)
        insert_round(conn, guild_id=GUILD_ID, submitter_id=1002, answer_id=1002)

    cog = _make_cog(str(sync_db_path))
    await cog.cog_load()

    assert cog.bot.add_view.call_count == 2
```

- [ ] **Step 2: Run to confirm it passes**

```
pytest tests/cogs/test_guess_submit.py::test_cog_load_registers_game_views_from_db -v
```
Expected: PASS.

- [ ] **Step 3: Run full suite — confirm no regressions**

```
pytest tests/unit/ tests/components/ tests/cogs/ -q
```
Expected: all green, 0 failures.

- [ ] **Step 4: Final commit**

```bash
git add tests/cogs/test_guess_submit.py
git commit -m "test(guess): cog_load view re-registration test + suite check"
```

---

## Self-Review Checklist

After all tasks complete, verify:

- [ ] `/guess submit` with no Guess role → ephemeral rejection
- [ ] `/guess submit` with unconfigured guess_channel_id=0 → ephemeral rejection
- [ ] `/guess submit` with non-image MIME → ephemeral rejection
- [ ] `/guess submit` with oversized file → ephemeral rejection
- [ ] `/guess submit` with small dimensions → ephemeral rejection
- [ ] `/guess submit` with no NudeNet detections → ephemeral "couldn't find crop region"
- [ ] `/guess submit` success → public game message in guess channel + ephemeral preview
- [ ] Re-roll button cycles crops, updates game message, disables at max
- [ ] Guess button on game message → ephemeral dropdown with guess-role members
- [ ] Correct first guess → game message edited with solve info; ephemeral "Correct!"
- [ ] Correct late guess → ephemeral "someone already solved"
- [ ] Wrong guess → ephemeral "Not it"
- [ ] Submitter clicking Guess on own round → ephemeral rejection
- [ ] Bot restart → `cog_load` re-registers GameViews; Guess buttons still work
- [ ] `pytest tests/unit/ tests/components/ tests/cogs/ -q` → all green

## Known Limitations (defer to future phases)

- Guess dropdown caps at 25 members (Discord limit); pagination not implemented
- Search-by-name modal not implemented
- Per-user guess cooldown tracked in DB but not enforced in this phase
- `/guess optin` / `/guess optout` not implemented (assign Guess role manually for now)
- Re-roll `guess_cache` file is updated to the latest crop but only the first crop's CDN URL is stored — the CDN URL updates are a future task
- `answer_id` is always `submitter_id` (v1 assumption from spec)
