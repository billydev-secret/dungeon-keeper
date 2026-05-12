"""Veil cog — NSFW guessing game (Phase 2)."""
from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import time
from pathlib import Path
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from db_utils import open_db
from services.veil_models import BoundingBox, VeilConfig, VeilGuess, VeilRound
from services.veil_pipeline import (
    compute_padded_crop,
    enforce_min_size,
    move_crop_box,
    run_pipeline,
    zoom_crop_box,
)
from services.veil_crop_renderer import render_crop, render_crop_editor
from services.quote_renderer import render_quote
from services.veil_repo import (
    count_guesses_for_round,
    count_unique_guessers_for_round,
    count_user_guesses_for_round,
    flag_user_open_rounds_optout,
    get_last_guess_by_user_for_round,
    get_round,
    get_unsolved_round_ids,
    get_veil_config,
    insert_audit_event,
    insert_guess,
    insert_round,
    mark_round_solved,
    set_round_original_path,
    set_round_reroll_count,
    set_veil_config_value,
    soft_delete_round,
    update_round_message,
)

# Hard cap on per-(user, round) guesses — kills brute-force-by-dropdown.
MAX_GUESSES_PER_USER_ROUND = 5

# Maximum number of persistent GameViews to re-register at startup. Bounds the
# discord.py view-matching cost as the round backlog grows. Unsolved rounds
# only — solved rounds use a "Guess late" button that's fun-loop polish.
_COG_LOAD_VIEW_CAP = 1000

if TYPE_CHECKING:
    from app_context import Bot

log = logging.getLogger("dungeonkeeper.veil")

# Originals are persisted here per-round and deleted after the first correct
# guess reveals them. Submitters are warned at submit time that the file is
# kept until the round is solved.
_VEIL_ORIG_DIR = Path("veil_cache") / "orig"


# ── Pure validation helpers (module-level so they're patchable in tests) ─────

def _has_veil_role(member: Any, veil_role_id: int) -> bool:
    """Fail closed: an unconfigured role (id 0) must never grant access."""
    if veil_role_id == 0:
        return False
    return any(r.id == veil_role_id for r in member.roles)


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

def _load_config(db_path: Path, guild_id: int) -> VeilConfig:
    with open_db(db_path) as conn:
        return get_veil_config(conn, guild_id)


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
    round_type: str = "photo",
    confession_text: str = "",
    confession_prompt_text: str = "",
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
            round_type=round_type,
            confession_text=confession_text,
            confession_prompt_text=confession_prompt_text,
        )


def _do_update_round_message(
    db_path: Path, round_id: int, message_id: int, crop_url: str, crop_path: str
) -> None:
    with open_db(db_path) as conn:
        update_round_message(conn, round_id, message_id=message_id, crop_url=crop_url, crop_path=crop_path)


def _do_set_reroll_count(db_path: Path, round_id: int, count: int) -> None:
    with open_db(db_path) as conn:
        set_round_reroll_count(conn, round_id, count)


def _do_set_original_path(db_path: Path, round_id: int, original_path: str) -> None:
    with open_db(db_path) as conn:
        set_round_original_path(conn, round_id, original_path)


def _do_load_round(db_path: Path, round_id: int) -> VeilRound | None:
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


def _do_get_last_guess(
    db_path: Path, round_id: int, guesser_id: int
) -> VeilGuess | None:
    with open_db(db_path) as conn:
        return get_last_guess_by_user_for_round(conn, round_id, guesser_id)


def _do_mark_solved(
    db_path: Path, round_id: int, *, solver_id: int
) -> tuple[int, int, int]:
    """Returns (rowcount, guess_count, unique_count). rowcount==0 means we lost
    the race — another correct guess marked solved first."""
    with open_db(db_path) as conn:
        guess_count = count_guesses_for_round(conn, round_id)
        unique_count = count_unique_guessers_for_round(conn, round_id)
        rowcount = mark_round_solved(
            conn, round_id,
            solver_id=solver_id,
            guesses_to_solve=guess_count,
            unique_guessers_to_solve=unique_count,
        )
    return rowcount, guess_count, unique_count


def _do_set_config(db_path: Path, guild_id: int, key: str, value: str) -> None:
    with open_db(db_path) as conn:
        set_veil_config_value(conn, guild_id, key, value)


def _do_load_unsolved_round_ids(db_path: Path, *, limit: int) -> list[int]:
    with open_db(db_path) as conn:
        return get_unsolved_round_ids(conn, limit=limit)


def _do_flag_user_open_rounds_optout(
    db_path: Path, *, guild_id: int, user_id: int
) -> int:
    with open_db(db_path) as conn:
        return flag_user_open_rounds_optout(conn, guild_id=guild_id, user_id=user_id)


def _do_soft_delete_round(db_path: Path, round_id: int) -> None:
    with open_db(db_path) as conn:
        soft_delete_round(conn, round_id)


def _do_count_user_guesses(db_path: Path, round_id: int, guesser_id: int) -> int:
    with open_db(db_path) as conn:
        return count_user_guesses_for_round(conn, round_id, guesser_id)


def _do_count_guesses_for_round(db_path: Path, round_id: int) -> int:
    with open_db(db_path) as conn:
        return count_guesses_for_round(conn, round_id)


def _do_count_unique_guessers_for_round(db_path: Path, round_id: int) -> int:
    with open_db(db_path) as conn:
        return count_unique_guessers_for_round(conn, round_id)


def _do_audit(
    db_path: Path,
    *,
    guild_id: int,
    actor_id: int,
    action: str,
    round_id: int | None = None,
    details: dict | None = None,
) -> None:
    """Best-effort audit write. Logs and swallows DB errors so the audit log
    never blocks user-facing flows."""
    try:
        with open_db(db_path) as conn:
            insert_audit_event(
                conn,
                guild_id=guild_id,
                actor_id=actor_id,
                action=action,
                round_id=round_id,
                details=details,
            )
    except Exception:
        log.exception("veil audit write failed for action=%s round=%s", action, round_id)


# ── Embed helpers ─────────────────────────────────────────────────────────────

def _game_embed(round_id: int) -> discord.Embed:
    return discord.Embed(
        title=f"Round #{round_id}",
        color=discord.Color.from_rgb(80, 20, 100),
    )


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


# ── Views ─────────────────────────────────────────────────────────────────────

SELECT_TIMEOUT_SECONDS = 60


class GuessSelectView(discord.ui.View):
    """Ephemeral view shown when a user clicks the Guess button."""

    def __init__(
        self,
        bot: "Bot",
        round_id: int,
        veil_members: Sequence[discord.Member],
        game_message: discord.Message,
        *,
        cooldown_seconds: int = 0,
        guess_placeholder: str = "Who is in this photo?",
    ) -> None:
        super().__init__(timeout=SELECT_TIMEOUT_SECONDS)
        self.bot = bot
        self.round_id = round_id
        self.game_message = game_message
        self.cooldown_seconds = cooldown_seconds

        options = [
            discord.SelectOption(label=m.display_name[:100], value=str(m.id))
            for m in veil_members[:25]
        ]
        self._select: discord.ui.Select = discord.ui.Select(  # type: ignore[type-arg]
            placeholder=guess_placeholder,
            options=options,
            min_values=1,
            max_values=1,
        )
        self._select.callback = self._on_select
        self.add_item(self._select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        # Ack within Discord's 3s budget before any DB / file / Discord-edit work.
        await interaction.response.defer(ephemeral=True)

        guessed_user_id = int(self._select.values[0])
        db_path = self.bot.ctx.db_path

        prior_guesses = await asyncio.to_thread(
            _do_count_user_guesses, db_path, self.round_id, interaction.user.id
        )
        if prior_guesses >= MAX_GUESSES_PER_USER_ROUND:
            self._select.disabled = True
            guild_id = interaction.guild.id if interaction.guild else 0
            await asyncio.to_thread(
                _do_audit, db_path,
                guild_id=guild_id, actor_id=interaction.user.id,
                action="guess_cap_hit", round_id=self.round_id,
                details={"prior_guesses": prior_guesses},
            )
            await interaction.edit_original_response(
                content=(
                    f"You're out of guesses on this round "
                    f"(cap: {MAX_GUESSES_PER_USER_ROUND})."
                ),
                view=self,
            )
            return

        if self.cooldown_seconds > 0:
            last_guess = await asyncio.to_thread(
                _do_get_last_guess, db_path, self.round_id, interaction.user.id
            )
            if last_guess is not None:
                remaining = self.cooldown_seconds - (time.time() - last_guess.created_at)
                if remaining > 0:
                    ready_at = int(last_guess.created_at) + self.cooldown_seconds
                    await interaction.edit_original_response(
                        content=f"⏳ On cooldown — you can guess again <t:{ready_at}:R>.",
                        view=self,
                    )
                    return

        round_row = await asyncio.to_thread(_do_load_round, db_path, self.round_id)
        if round_row is None:
            await interaction.edit_original_response(content="Round not found.", view=None)
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

        if correct and round_row.solved_at is None:
            self._select.disabled = True
            rowcount, guess_count, unique_count = await asyncio.to_thread(
                _do_mark_solved, db_path, self.round_id, solver_id=interaction.user.id
            )
            if rowcount == 0:
                # Lost the race — a concurrent correct guess marked solved first.
                await interaction.edit_original_response(
                    content="✅ Correct — but someone already solved this one.",
                    view=self,
                )
                return
            answer_mention = f"<@{round_row.answer_id}>"
            submitter_mention = f"<@{round_row.submitter_id}>"
            solved_emb = _solved_embed(
                self.round_id, answer_mention, submitter_mention,
                interaction.user.mention, guess_count, unique_count,
            )
            new_game_view = GameView(self.bot, self.round_id, solved=True)

            full_attachments: list[discord.File] = []
            orig_path: Path | None = None
            if round_row.original_path:
                orig_path = Path(round_row.original_path)
                if orig_path.exists():
                    suffix = orig_path.suffix or ".jpg"
                    full_bytes = await asyncio.to_thread(orig_path.read_bytes)
                    full_attachments.append(
                        discord.File(
                            io.BytesIO(full_bytes),
                            filename=f"SPOILER_veil_full{suffix}",
                        )
                    )

            if full_attachments:
                await self.game_message.edit(
                    embed=solved_emb,
                    view=new_game_view,
                    attachments=full_attachments,
                )
            else:
                await self.game_message.edit(embed=solved_emb, view=new_game_view)

            if orig_path is not None:
                await asyncio.to_thread(orig_path.unlink, missing_ok=True)
                await asyncio.to_thread(_do_set_original_path, db_path, self.round_id, "")
            guild_id = interaction.guild.id if interaction.guild else round_row.guild_id
            await asyncio.to_thread(
                _do_audit, db_path,
                guild_id=guild_id, actor_id=interaction.user.id,
                action="solve", round_id=self.round_id,
                details={
                    "guesses_to_solve": guess_count,
                    "unique_guessers": unique_count,
                },
            )
            await interaction.edit_original_response(
                content=f"✅ **Correct!** You solved Round #{self.round_id}!",
                view=self,
            )
        elif correct:
            self._select.disabled = True
            await interaction.edit_original_response(
                content="✅ Correct — but someone already solved this one.",
                view=self,
            )
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
                        "veil: chip counter bump failed for round %d", self.round_id
                    )
            # Extend view lifetime so the user can guess again after cooldown expires.
            if self.cooldown_seconds > 0:
                self.timeout = self.cooldown_seconds + SELECT_TIMEOUT_SECONDS
            await interaction.edit_original_response(
                content="❌ Not it. Keep trying!",
                view=self,
            )


class GameView(discord.ui.View):
    """Persistent public view attached to a game message."""

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
            custom_id=f"veil_guess:{round_id}",
            row=0,
        )
        btn.callback = self._guess_callback
        self.add_item(btn)

        if not solved:
            count_chip: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
                label=f"Guesses: {guess_count}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"veil_chip_count:{round_id}",
                disabled=True,
                row=1,
            )
            # ▒ is U+2592 MEDIUM SHADE × 7 — a redaction bar, not a font glitch.
            submitter_chip: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
                label="Submitted by ▒▒▒▒▒▒▒",
                style=discord.ButtonStyle.secondary,
                custom_id=f"veil_chip_submitter:{round_id}",
                disabled=True,
                row=1,
            )
            self.add_item(count_chip)
            self.add_item(submitter_chip)

    async def _guess_callback(self, interaction: discord.Interaction) -> None:
        assert interaction.guild and interaction.message
        db_path = self.bot.ctx.db_path
        config = await asyncio.to_thread(_load_config, db_path, interaction.guild.id)

        round_row = await asyncio.to_thread(_do_load_round, db_path, self.round_id)
        if round_row and round_row.answer_optout:
            await interaction.response.send_message(
                "This round is no longer solvable — the answer opted out.",
                ephemeral=True,
            )
            return
        if round_row and interaction.user.id == round_row.submitter_id:
            await interaction.response.send_message(
                "You can't guess on your own round.", ephemeral=True
            )
            return

        veil_role = interaction.guild.get_role(config.veil_role_id)
        if veil_role is None:
            await interaction.response.send_message(
                "Veil role not found — ask an admin to configure it.", ephemeral=True
            )
            return

        veil_members = [m for m in veil_role.members if not m.bot]
        if not veil_members:
            await interaction.response.send_message(
                "No opted-in members to guess from.", ephemeral=True
            )
            return

        placeholder = (
            "Who wrote this?"
            if round_row and round_row.round_type == "confession"
            else "Who is in this photo?"
        )
        await interaction.response.send_message(
            f"Who do you think this is? *(prompt expires in {SELECT_TIMEOUT_SECONDS}s)*",
            view=GuessSelectView(
                self.bot, self.round_id, veil_members, interaction.message,
                cooldown_seconds=config.guess_cooldown_seconds,
                guess_placeholder=placeholder,
            ),
            ephemeral=True,
        )


CROP_EDITOR_ZOOM_IN: float = 0.8
CROP_EDITOR_ZOOM_OUT: float = 1.25


class CropEditorView(discord.ui.View):
    """3×3 button grid for interactive crop framing.

    Row 0:  [🔍+]  [↑]     [🔍−]
    Row 1:  [← ]  [↓]     [→  ]
    Row 2:  [Post] [Auto]  [✗  ]

    Auto cycles through pipeline detections (first press → top candidate,
    subsequent presses cycle, wraps around). Disabled when no candidates.
    Step size is 1/5 of the current crop box so precision scales with zoom level.
    The round is only inserted to DB when Post is clicked.
    """

    def __init__(
        self,
        bot: "Bot",
        image_bytes: bytes,
        img_w: int,
        img_h: int,
        crop_box: BoundingBox,
        guild_id: int,
        veil_channel_id: int,
        *,
        submitter_id: int,
        answer_id: int,
        difficulty: str,
        candidate_count: int,
        veil_role_id: int = 0,
        original_bytes: bytes = b"",
        original_ext: str = ".jpg",
        candidate_boxes: list[BoundingBox] | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.image_bytes = image_bytes
        self.img_w = img_w
        self.img_h = img_h
        self.crop_box = crop_box
        self.guild_id = guild_id
        self.veil_channel_id = veil_channel_id
        self._submitter_id = submitter_id
        self._answer_id = answer_id
        self._difficulty = difficulty
        self._candidate_count = candidate_count
        self._veil_role_id = veil_role_id
        self._original_bytes = original_bytes
        self._original_ext = original_ext
        self._post_lock = asyncio.Lock()
        self._posted = False
        self._candidate_boxes: list[BoundingBox] = candidate_boxes or []
        self._candidate_idx = -1  # -1 so first Auto press snaps to index 0

        B = discord.ui.Button  # type: ignore[type-arg]

        # Row 0: zoom-in | up | zoom-out
        z_in: discord.ui.Button = B(label="  🔍+  ", style=discord.ButtonStyle.secondary, row=0)  # type: ignore[type-arg]
        z_in.callback = self._on_zoom_in
        self.add_item(z_in)
        up: discord.ui.Button = B(label="    ↑    ", style=discord.ButtonStyle.secondary, row=0)  # type: ignore[type-arg]
        up.callback = self._on_up
        self.add_item(up)
        z_out: discord.ui.Button = B(label="  🔍−  ", style=discord.ButtonStyle.secondary, row=0)  # type: ignore[type-arg]
        z_out.callback = self._on_zoom_out
        self.add_item(z_out)

        # Row 1: left | down | right
        left: discord.ui.Button = B(label="    ←    ", style=discord.ButtonStyle.secondary, row=1)  # type: ignore[type-arg]
        left.callback = self._on_left
        self.add_item(left)
        down: discord.ui.Button = B(label="    ↓    ", style=discord.ButtonStyle.secondary, row=1)  # type: ignore[type-arg]
        down.callback = self._on_down
        self.add_item(down)
        right: discord.ui.Button = B(label="    →    ", style=discord.ButtonStyle.secondary, row=1)  # type: ignore[type-arg]
        right.callback = self._on_right
        self.add_item(right)

        # Row 2: post | auto | cancel
        post: discord.ui.Button = B(label=" ✓ Post ", style=discord.ButtonStyle.success, row=2)  # type: ignore[type-arg]
        post.callback = self._on_post
        self.add_item(post)
        n = len(self._candidate_boxes)
        auto: discord.ui.Button = B(label="  Auto  ", style=discord.ButtonStyle.primary, disabled=n == 0, row=2)  # type: ignore[type-arg]
        auto.callback = self._on_auto
        self._auto_btn = auto
        self.add_item(auto)
        cancel: discord.ui.Button = B(label="    ✗    ", style=discord.ButtonStyle.danger, row=2)  # type: ignore[type-arg]
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def _rerender(self, interaction: discord.Interaction) -> None:
        editor_bytes = await asyncio.to_thread(
            render_crop_editor, self.image_bytes, self.crop_box
        )
        preview_file = discord.File(io.BytesIO(editor_bytes), filename="preview.jpg")
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Crop editor",
                description="Move/zoom the red box or press Auto to snap to a detection, then ✓ Post",
            ).set_image(url="attachment://preview.jpg"),
            attachments=[preview_file],
            view=self,
        )

    async def _on_up(self, interaction: discord.Interaction) -> None:
        self.crop_box = move_crop_box(self.crop_box, 0, -self.crop_box.height / 5, self.img_w, self.img_h)
        await self._rerender(interaction)

    async def _on_down(self, interaction: discord.Interaction) -> None:
        self.crop_box = move_crop_box(self.crop_box, 0, self.crop_box.height / 5, self.img_w, self.img_h)
        await self._rerender(interaction)

    async def _on_left(self, interaction: discord.Interaction) -> None:
        self.crop_box = move_crop_box(self.crop_box, -self.crop_box.width / 5, 0, self.img_w, self.img_h)
        await self._rerender(interaction)

    async def _on_right(self, interaction: discord.Interaction) -> None:
        self.crop_box = move_crop_box(self.crop_box, self.crop_box.width / 5, 0, self.img_w, self.img_h)
        await self._rerender(interaction)

    async def _on_zoom_in(self, interaction: discord.Interaction) -> None:
        self.crop_box = zoom_crop_box(self.crop_box, CROP_EDITOR_ZOOM_IN, self.img_w, self.img_h)
        await self._rerender(interaction)

    async def _on_zoom_out(self, interaction: discord.Interaction) -> None:
        self.crop_box = zoom_crop_box(self.crop_box, CROP_EDITOR_ZOOM_OUT, self.img_w, self.img_h)
        await self._rerender(interaction)

    async def _on_auto(self, interaction: discord.Interaction) -> None:
        if not self._candidate_boxes:
            return
        self._candidate_idx = (self._candidate_idx + 1) % len(self._candidate_boxes)
        self.crop_box = self._candidate_boxes[self._candidate_idx]
        await self._rerender(interaction)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Submission cancelled.", embed=None, attachments=[], view=None
        )

    async def _on_post(self, interaction: discord.Interaction) -> None:
        assert interaction.guild
        async with self._post_lock:
            if self._posted:
                await interaction.response.send_message("Already posted.", ephemeral=True)
                return
            self._posted = True

        await interaction.response.defer(ephemeral=True)

        veil_channel = interaction.guild.get_channel(self.veil_channel_id)
        if veil_channel is None or not isinstance(
            veil_channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread)
        ):
            await interaction.followup.send(
                "Veil channel not found — ask an admin to check the config.", ephemeral=True
            )
            return

        if hasattr(veil_channel, "is_nsfw") and not veil_channel.is_nsfw():
            await interaction.followup.send(
                f"{veil_channel.mention} is no longer NSFW-flagged. "
                "Veil refuses to post explicit content in non-age-gated channels.",
                ephemeral=True,
            )
            return

        crop_bytes = await asyncio.to_thread(render_crop, self.image_bytes, self.crop_box)

        db_path = self.bot.ctx.db_path
        round_id = await asyncio.to_thread(
            _do_insert_round,
            db_path,
            guild_id=self.guild_id,
            submitter_id=self._submitter_id,
            answer_id=self._answer_id,
            channel_id=self.veil_channel_id,
            difficulty=self._difficulty,
            allow_reuse=False,
            candidate_count=self._candidate_count,
        )

        if self._original_bytes:
            orig_path = _VEIL_ORIG_DIR / f"{round_id}{self._original_ext}"

            def _write_original() -> None:
                _VEIL_ORIG_DIR.mkdir(parents=True, exist_ok=True)
                orig_path.write_bytes(self._original_bytes)

            await asyncio.to_thread(_write_original)
            await asyncio.to_thread(_do_set_original_path, db_path, round_id, str(orig_path))

        crop_file = discord.File(io.BytesIO(crop_bytes), filename="SPOILER_veil_crop.jpg")
        game_view = GameView(self.bot, round_id)
        self.bot.add_view(game_view)
        role_ping = f"<@&{self._veil_role_id}>" if self._veil_role_id else None
        game_msg = await veil_channel.send(
            content=role_ping,
            embed=_game_embed(round_id),
            file=crop_file,
            view=game_view,
        )

        crop_url = game_msg.attachments[0].url if game_msg.attachments else ""
        await asyncio.to_thread(
            _do_update_round_message, db_path, round_id, game_msg.id, crop_url, ""
        )

        await asyncio.to_thread(
            _do_audit, db_path,
            guild_id=self.guild_id, actor_id=self._submitter_id,
            action="submit", round_id=round_id,
            details={"difficulty": self._difficulty},
        )

        await interaction.edit_original_response(
            content=f"✅ Posted to {veil_channel.mention}!",
            embed=None,
            attachments=[],
            view=None,
        )

        try:
            await _repost_prompt(self.bot, veil_channel, self.guild_id)
        except Exception:
            log.exception("veil: prompt repost after game post failed for guild %d", self.guild_id)


# ── Sticky channel prompt ────────────────────────────────────────────────────

# Trailing-edge debounce: after the last channel message, wait this long before
# re-posting. Cancels and reschedules on each new message so the prompt lands
# under the final message of a burst rather than flickering through each one.
PROMPT_REPOST_DELAY_SEC = 2.0

_PROMPT_HOW_TO_PLAY = (
    "**How Veil works**\n"
    "Members of the Veil pool submit anonymized NSFW images. Everyone else "
    "guesses who's in the photo.\n"
    "\n"
    "• **Submit:** `/veil submit` with an image. The bot crops it and you "
    "pick a crop, then post it anonymously.\n"
    "• **Guess:** click *Guess* on a posted round and pick a name. The chip "
    "below the image counts total guesses on the round.\n"
    "• **Solve:** the first correct guess wins, reveals the full image as a "
    "spoiler, and credits the submitter.\n"
    "• **Join the pool:** `/veil optin` to add yourself. Only pool members "
    "can be answers and submit images."
)

_PROMPT_SUBMIT_INSTRUCTIONS = (
    "Use `/veil submit` and attach an NSFW image. The bot will run an "
    "auto-crop pipeline and let you pick from a few crops; click **Post** "
    "when you're happy with one. You'll get a preview before it goes live."
)


def _prompt_embed() -> discord.Embed:
    return discord.Embed(
        title="🎭 Veil",
        description=(
            "Submit anonymized NSFW images for everyone to guess. "
            "Click below to play."
        ),
        color=discord.Color.from_rgb(80, 20, 100),
    )


class VeilPromptView(discord.ui.View):
    """Persistent view attached to the channel-bottom prompt message."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

        submit_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="🎭 Submit Veil",
            style=discord.ButtonStyle.primary,
            custom_id="veil_prompt_submit",
        )
        submit_btn.callback = self._on_submit
        self.add_item(submit_btn)

        help_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="❓ How to Play",
            style=discord.ButtonStyle.secondary,
            custom_id="veil_prompt_help",
        )
        help_btn.callback = self._on_help
        self.add_item(help_btn)

    async def _on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            _PROMPT_SUBMIT_INSTRUCTIONS, ephemeral=True
        )

    async def _on_help(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            _PROMPT_HOW_TO_PLAY, ephemeral=True
        )


async def _repost_prompt(
    bot: "Bot",
    channel: discord.TextChannel | discord.VoiceChannel | discord.Thread,
    guild_id: int,
) -> None:
    """Delete the previous prompt (if any), post a fresh one, persist its ID.

    Best-effort: missing/forbidden prior messages are tolerated. Any failure
    posting the new prompt logs and falls through — the channel just won't
    have a prompt until the next attempt.
    """
    db_path = bot.ctx.db_path
    config = await asyncio.to_thread(_load_config, db_path, guild_id)

    if config.prompt_message_id:
        try:
            old = await channel.fetch_message(config.prompt_message_id)
            await old.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

    try:
        new_msg = await channel.send(embed=_prompt_embed(), view=VeilPromptView())
    except discord.HTTPException:
        log.exception("veil: failed to post channel prompt in guild %d", guild_id)
        return

    await asyncio.to_thread(
        _do_set_config, db_path, guild_id, "veil_prompt_message_id", str(new_msg.id)
    )


# ── Confession preview ───────────────────────────────────────────────────────

class ConfessionPreviewView(discord.ui.View):
    """Ephemeral preview shown before a text confession is posted publicly."""

    def __init__(
        self,
        bot: "Bot",
        text: str,
        guild_id: int,
        veil_channel_id: int,
        submitter_id: int,
        veil_role_id: int,
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self._text = text
        self._guild_id = guild_id
        self._veil_channel_id = veil_channel_id
        self._submitter_id = submitter_id
        self._veil_role_id = veil_role_id
        self._post_lock = asyncio.Lock()
        self._posted = False

        post: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label=" ✓ Post ",
            style=discord.ButtonStyle.success,
        )
        post.callback = self._on_post
        self.add_item(post)

        cancel: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="  ✗ Cancel  ",
            style=discord.ButtonStyle.danger,
        )
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Submission cancelled.", embed=None, attachments=[], view=None
        )

    async def _on_post(self, interaction: discord.Interaction) -> None:
        assert interaction.guild
        async with self._post_lock:
            if self._posted:
                await interaction.response.send_message("Already posted.", ephemeral=True)
                return
            self._posted = True

        await interaction.response.defer(ephemeral=True)

        veil_channel = interaction.guild.get_channel(self._veil_channel_id)
        if veil_channel is None or not isinstance(
            veil_channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread)
        ):
            await interaction.followup.send(
                "Veil channel not found — ask an admin to check the config.", ephemeral=True
            )
            return

        db_path = self.bot.ctx.db_path
        round_id = await asyncio.to_thread(
            _do_insert_round,
            db_path,
            guild_id=self._guild_id,
            submitter_id=self._submitter_id,
            answer_id=self._submitter_id,
            channel_id=self._veil_channel_id,
            difficulty="confession",
            allow_reuse=False,
            candidate_count=0,
            round_type="confession",
            confession_text=self._text,
        )

        card_bytes = await asyncio.to_thread(
            render_quote, self._text, footer=f"Veil #{round_id}"
        )
        card_file = discord.File(io.BytesIO(card_bytes), filename="veil_confession.jpg")

        embed = discord.Embed(
            title=f"Round #{round_id}",
            color=discord.Color.from_rgb(80, 20, 100),
        )

        game_view = GameView(self.bot, round_id)
        self.bot.add_view(game_view)

        role_ping = f"<@&{self._veil_role_id}>" if self._veil_role_id else None
        game_msg = await veil_channel.send(
            content=role_ping,
            embed=embed,
            file=card_file,
            view=game_view,
        )

        crop_url = game_msg.attachments[0].url if game_msg.attachments else ""
        await asyncio.to_thread(
            _do_update_round_message, db_path, round_id, game_msg.id, crop_url, ""
        )
        await asyncio.to_thread(
            _do_audit, db_path,
            guild_id=self._guild_id, actor_id=self._submitter_id,
            action="confession_submit", round_id=round_id,
        )

        await interaction.edit_original_response(
            content=f"✅ Posted to {veil_channel.mention}!",
            embed=None,
            attachments=[],
            view=None,
        )

        try:
            await _repost_prompt(self.bot, veil_channel, self._guild_id)
        except Exception:
            log.exception("veil: prompt repost after confession post failed for guild %d", self._guild_id)


# ── VeilCog ──────────────────────────────────────────────────────────────────

class VeilCog(commands.Cog):
    veil = app_commands.Group(
        name="veil",
        description="Veil guessing game commands.",
        guild_only=True,
    )

    def __init__(self, bot: "Bot") -> None:
        self.bot = bot
        # Per-guild debounce tasks for the sticky channel prompt re-poster.
        self._pending_prompt_reposts: dict[int, asyncio.Task[None]] = {}
        super().__init__()

    async def cog_load(self) -> None:
        """Re-register persistent GameViews for unsolved rounds (capped) and
        the channel-prompt view."""
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
        self.bot.add_view(VeilPromptView())
        log.info("veil: re-registered %d persistent GameViews (cap %d)",
                 len(round_ids), _COG_LOAD_VIEW_CAP)

    async def cog_unload(self) -> None:
        for task in self._pending_prompt_reposts.values():
            if not task.done():
                task.cancel()
        self._pending_prompt_reposts.clear()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Sticky-prompt re-poster: schedule a debounced repost when activity
        lands in the configured veil channel. Bot's own messages and DMs are
        ignored. The prompt itself is a bot message — ignoring those prevents
        a feedback loop."""
        if message.author.bot or message.guild is None:
            return
        db_path = self.bot.ctx.db_path
        config = await asyncio.to_thread(_load_config, db_path, message.guild.id)
        if config.veil_channel_id == 0 or message.channel.id != config.veil_channel_id:
            return
        if not isinstance(
            message.channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread)
        ):
            return

        existing = self._pending_prompt_reposts.get(message.guild.id)
        if existing and not existing.done():
            existing.cancel()
        self._pending_prompt_reposts[message.guild.id] = asyncio.create_task(
            self._delayed_repost_prompt(message.guild.id, message.channel)
        )

    async def _delayed_repost_prompt(
        self,
        guild_id: int,
        channel: discord.TextChannel | discord.VoiceChannel | discord.Thread,
    ) -> None:
        try:
            await asyncio.sleep(PROMPT_REPOST_DELAY_SEC)
        except asyncio.CancelledError:
            return
        try:
            await _repost_prompt(self.bot, channel, guild_id)
        except Exception:
            log.exception("veil: sticky prompt repost failed for guild %d", guild_id)

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        """When a member loses the Veil role, flag their open rounds as
        answer_optout so they can never be guessed again — even if they
        re-acquire the role later."""
        before_role_ids = {r.id for r in before.roles}
        after_role_ids = {r.id for r in after.roles}
        removed = before_role_ids - after_role_ids
        if not removed:
            return
        db_path = self.bot.ctx.db_path
        config = await asyncio.to_thread(_load_config, db_path, after.guild.id)
        if config.veil_role_id == 0 or config.veil_role_id not in removed:
            return
        flagged = await asyncio.to_thread(
            _do_flag_user_open_rounds_optout,
            db_path,
            guild_id=after.guild.id,
            user_id=after.id,
        )
        if flagged:
            log.info(
                "veil: %d open rounds flagged answer_optout for user %d (role removed)",
                flagged, after.id,
            )

    @veil.command(name="submit", description="Submit an image to start a Veil round.")
    @app_commands.describe(
        image="The NSFW image to submit",
    )
    async def veil_submit(
        self,
        interaction: discord.Interaction,
        image: discord.Attachment,
    ) -> None:
        assert interaction.guild
        await interaction.response.defer(ephemeral=True)

        db_path = self.bot.ctx.db_path
        config = await asyncio.to_thread(_load_config, db_path, interaction.guild.id)

        if config.veil_role_id == 0:
            await interaction.followup.send(
                "Veil role is not configured. Ask an admin to run `/veil setup`.",
                ephemeral=True,
            )
            return

        if config.veil_channel_id == 0:
            await interaction.followup.send(
                "Veil channel is not configured. Ask an admin to run `/veil setup`.", ephemeral=True
            )
            return

        member = interaction.guild.get_member(interaction.user.id)
        if not member or not _has_veil_role(member, config.veil_role_id):
            await interaction.followup.send(
                "You need the Veil role to submit.", ephemeral=True
            )
            return

        if not _validate_mime(image.content_type):
            await interaction.followup.send("Please submit an image file.", ephemeral=True)
            return

        if not _validate_size(image.size, config.max_image_size_mb):
            await interaction.followup.send(
                f"Image too large. Maximum is {config.max_image_size_mb} MB.", ephemeral=True
            )
            return

        image_bytes = await image.read()

        dim_ok, img_w, img_h = await asyncio.to_thread(
            _validate_dimensions, image_bytes, config.min_image_dimension_px
        )
        if not dim_ok:
            await interaction.followup.send(
                f"Image too small. Minimum dimension is {config.min_image_dimension_px}px.", ephemeral=True
            )
            return

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
                candidate_count=10,
            )
        finally:
            tmp_path.unlink(missing_ok=True)

        sorted_cands = sorted(pipeline_result.candidates, key=lambda d: d.score, reverse=True)
        candidate_boxes: list[BoundingBox] = []
        for det in sorted_cands:
            _exp = enforce_min_size(compute_padded_crop(det.box, config.crop_difficulty, img_w, img_h))
            candidate_boxes.append(BoundingBox(
                max(0.0, _exp.x1), max(0.0, _exp.y1),
                min(float(img_w), _exp.x2), min(float(img_h), _exp.y2),
            ))

        if candidate_boxes:
            initial_crop = candidate_boxes[0]
            embed_desc = "Move/zoom the red box or press Auto to snap to a detection, then ✓ Post"
        else:
            # No detections — default to centre 60% so the user has something to work with
            mx, my = img_w * 0.2, img_h * 0.2
            _exp = enforce_min_size(BoundingBox(mx, my, img_w - mx, img_h - my))
            initial_crop = BoundingBox(
                max(0.0, _exp.x1), max(0.0, _exp.y1),
                min(float(img_w), _exp.x2), min(float(img_h), _exp.y2),
            )
            embed_desc = "No detections found — manually frame your crop, then ✓ Post"

        editor_bytes = await asyncio.to_thread(render_crop_editor, image_bytes, initial_crop)
        original_ext = (Path(image.filename).suffix or ".jpg").lower()
        await interaction.followup.send(
            embed=discord.Embed(
                title="Crop editor",
                description=embed_desc,
            ).set_image(url="attachment://preview.jpg"),
            file=discord.File(io.BytesIO(editor_bytes), filename="preview.jpg"),
            view=CropEditorView(
                self.bot,
                image_bytes=image_bytes,
                img_w=img_w,
                img_h=img_h,
                crop_box=initial_crop,
                guild_id=interaction.guild.id,
                veil_channel_id=config.veil_channel_id,
                submitter_id=interaction.user.id,
                answer_id=interaction.user.id,
                difficulty=config.crop_difficulty,
                candidate_count=len(pipeline_result.candidates),
                veil_role_id=config.veil_role_id,
                original_bytes=image_bytes,
                original_ext=original_ext,
                candidate_boxes=candidate_boxes,
            ),
            ephemeral=True,
        )


    @veil.command(name="round", description="Inspect a Veil round (mods only).")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(round_id="Round ID to inspect")
    async def veil_round(
        self,
        interaction: discord.Interaction,
        round_id: int,
    ) -> None:
        assert interaction.guild
        await interaction.response.defer(ephemeral=True)

        member = interaction.guild.get_member(interaction.user.id)
        if not (member and member.guild_permissions.manage_guild):
            await interaction.followup.send(
                "Only mods (manage_guild permission) can inspect rounds.",
                ephemeral=True,
            )
            return

        db_path = self.bot.ctx.db_path
        round_row = await asyncio.to_thread(_do_load_round, db_path, round_id)
        if round_row is None or round_row.guild_id != interaction.guild.id:
            await interaction.followup.send(
                f"Round #{round_id} not found.", ephemeral=True
            )
            return

        guess_count = await asyncio.to_thread(
            _do_count_guesses_for_round, db_path, round_id
        )
        unique_count = await asyncio.to_thread(
            _do_count_unique_guessers_for_round, db_path, round_id
        )

        if round_row.deleted_at is not None:
            status = "🗑 Deleted"
        elif round_row.solved_at is not None:
            status = f"✅ Solved by <@{round_row.solver_id}>"
        else:
            status = "⏳ Open"

        embed = discord.Embed(
            title=f"Round #{round_row.id} — inspector",
            color=discord.Color.dark_grey(),
            description=(
                f"**Status:** {status}\n"
                f"**Submitter:** <@{round_row.submitter_id}>\n"
                f"**Answer:** <@{round_row.answer_id}>\n"
                f"**Difficulty:** {round_row.difficulty}\n"
                f"**Guesses:** {guess_count} ({unique_count} unique guessers)\n"
                f"**Re-rolls:** {round_row.reroll_count}\n"
                f"**Created:** <t:{int(round_row.created_at)}:R>"
            ),
        )
        if round_row.crop_url:
            embed.set_image(url=round_row.crop_url)
        await interaction.followup.send(embed=embed, ephemeral=True)


    @veil.command(name="delete", description="Delete a Veil round (submitter or mod only).")
    @app_commands.describe(round_id="Round ID to delete")
    async def veil_delete(
        self,
        interaction: discord.Interaction,
        round_id: int,
    ) -> None:
        assert interaction.guild
        await interaction.response.defer(ephemeral=True)

        db_path = self.bot.ctx.db_path
        round_row = await asyncio.to_thread(_do_load_round, db_path, round_id)
        if round_row is None or round_row.guild_id != interaction.guild.id:
            await interaction.followup.send(
                f"Round #{round_id} not found.", ephemeral=True
            )
            return

        if round_row.deleted_at is not None:
            await interaction.followup.send(
                f"Round #{round_id} is already deleted.", ephemeral=True
            )
            return

        is_submitter = interaction.user.id == round_row.submitter_id
        member = interaction.guild.get_member(interaction.user.id)
        is_mod = bool(member and member.guild_permissions.manage_guild)
        if not (is_submitter or is_mod):
            await interaction.followup.send(
                "Only the submitter or a mod can delete this round.", ephemeral=True
            )
            return

        await asyncio.to_thread(_do_soft_delete_round, db_path, round_id)
        await asyncio.to_thread(
            _do_audit, db_path,
            guild_id=interaction.guild.id, actor_id=interaction.user.id,
            action="delete", round_id=round_id,
            details={"by_mod": is_mod and not is_submitter},
        )

        if round_row.original_path:
            orig_path = Path(round_row.original_path)
            if orig_path.exists():
                await asyncio.to_thread(orig_path.unlink, missing_ok=True)

        if round_row.channel_id and round_row.message_id:
            channel = interaction.guild.get_channel(round_row.channel_id)
            if isinstance(
                channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread)
            ):
                try:
                    msg = await channel.fetch_message(round_row.message_id)
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass

        await interaction.followup.send(
            f"Round #{round_id} deleted.", ephemeral=True
        )


    @veil.command(name="optin", description="Join the Veil pool — add the Veil role to yourself.")
    async def veil_optin(self, interaction: discord.Interaction) -> None:
        assert interaction.guild
        await interaction.response.defer(ephemeral=True)

        db_path = self.bot.ctx.db_path
        config = await asyncio.to_thread(_load_config, db_path, interaction.guild.id)

        if config.veil_role_id == 0:
            await interaction.followup.send(
                "Veil role is not configured. Ask an admin to run `/veil setup`.",
                ephemeral=True,
            )
            return

        role = interaction.guild.get_role(config.veil_role_id)
        if role is None:
            await interaction.followup.send(
                "Veil role is configured but no longer exists. "
                "Ask an admin to re-run `/veil setup`.",
                ephemeral=True,
            )
            return

        member = interaction.guild.get_member(interaction.user.id)
        if member is None:
            await interaction.followup.send(
                "Couldn't find you in this guild.", ephemeral=True
            )
            return

        if _has_veil_role(member, config.veil_role_id):
            await interaction.followup.send(
                f"You're already in the Veil pool ({role.mention}).",
                ephemeral=True,
            )
            return

        try:
            await member.add_roles(role, reason="Veil opt-in")
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to add that role. "
                "Ask an admin to check my role permissions and hierarchy.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Welcome to the Veil pool. You can now submit images and be guessed at. "
            f"To leave, ask a mod to remove the {role.mention} role.",
            ephemeral=True,
        )


    @veil.command(name="prompt", description="Post the channel-bottom Submit/Help prompt now.")
    @app_commands.default_permissions(manage_guild=True)
    async def veil_prompt(self, interaction: discord.Interaction) -> None:
        assert interaction.guild
        await interaction.response.defer(ephemeral=True)

        db_path = self.bot.ctx.db_path
        config = await asyncio.to_thread(_load_config, db_path, interaction.guild.id)

        if config.veil_channel_id == 0:
            await interaction.followup.send(
                "Veil channel is not configured. Run `/veil setup` first.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(config.veil_channel_id)
        if not isinstance(
            channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread)
        ):
            await interaction.followup.send(
                "Configured Veil channel can't be posted to. "
                "Re-run `/veil setup`.",
                ephemeral=True,
            )
            return

        await _repost_prompt(self.bot, channel, interaction.guild.id)
        await interaction.followup.send(
            f"Prompt posted in {channel.mention}.", ephemeral=True
        )


    @veil.command(name="setup", description="Configure the Veil game channel and role.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        channel="The NSFW channel where game posts appear",
        role="Role required to submit images and act as guess answers",
    )
    async def veil_setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        role: discord.Role,
    ) -> None:
        assert interaction.guild
        await interaction.response.defer(ephemeral=True)

        if not channel.is_nsfw():
            await interaction.followup.send(
                f"{channel.mention} is not age-gated. Veil only posts in NSFW channels — "
                "enable the channel's NSFW flag and try again.",
                ephemeral=True,
            )
            return

        db_path = self.bot.ctx.db_path
        guild_id = interaction.guild.id

        await asyncio.to_thread(_do_set_config, db_path, guild_id, "veil_channel_id", str(channel.id))
        await asyncio.to_thread(_do_set_config, db_path, guild_id, "veil_role_id", str(role.id))

        await interaction.followup.send(
            f"Veil configured.\n- Game channel: {channel.mention}\n- Veil role: {role.mention}",
            ephemeral=True,
        )


    @veil.command(name="confess", description="Submit an anonymous text confession.")
    @app_commands.describe(text="Your anonymous confession.")
    async def veil_confess(
        self,
        interaction: discord.Interaction,
        text: str,
    ) -> None:
        assert interaction.guild
        await interaction.response.defer(ephemeral=True)

        db_path = self.bot.ctx.db_path
        config = await asyncio.to_thread(_load_config, db_path, interaction.guild.id)

        if config.veil_role_id == 0:
            await interaction.followup.send(
                "Veil role is not configured. Ask an admin to run `/veil setup`.",
                ephemeral=True,
            )
            return

        if config.veil_channel_id == 0:
            await interaction.followup.send(
                "Veil channel is not configured. Ask an admin to run `/veil setup`.",
                ephemeral=True,
            )
            return

        member = interaction.guild.get_member(interaction.user.id)
        if not member or not _has_veil_role(member, config.veil_role_id):
            await interaction.followup.send(
                "You need the Veil role to submit a confession.", ephemeral=True
            )
            return

        if not text.strip():
            await interaction.followup.send("Confession text can't be empty.", ephemeral=True)
            return

        preview_bytes = await asyncio.to_thread(render_quote, text)
        preview_file = discord.File(io.BytesIO(preview_bytes), filename="preview.jpg")

        await interaction.followup.send(
            embed=discord.Embed(
                title="Preview",
                description=(
                    "This is how your confession will appear. Click **Post** to publish it "
                    "anonymously, or **Cancel** to discard."
                ),
            ).set_image(url="attachment://preview.jpg"),
            file=preview_file,
            view=ConfessionPreviewView(
                self.bot,
                text=text,
                guild_id=interaction.guild.id,
                veil_channel_id=config.veil_channel_id,
                submitter_id=interaction.user.id,
                veil_role_id=config.veil_role_id,
            ),
            ephemeral=True,
        )


async def setup(bot: "Bot") -> None:
    await bot.add_cog(VeilCog(bot))
