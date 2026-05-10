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
from services.veil_models import VeilConfig, VeilGuess, VeilRound
from services.veil_pipeline import run_pipeline
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
            placeholder="Who is in this photo?",
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
                    await interaction.edit_original_response(
                        content=f"⏳ On cooldown — try again in {int(remaining) + 1}s.",
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

        self._select.disabled = True

        if correct and round_row.solved_at is None:
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

            await self.game_message.edit(
                embed=solved_emb,
                view=new_game_view,
                attachments=full_attachments,
            )

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
            await interaction.edit_original_response(
                content="✅ Correct — but someone already solved this one.",
                view=self,
            )
        else:
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

        await interaction.response.send_message(
            f"Who do you think this is? *(prompt expires in {SELECT_TIMEOUT_SECONDS}s)*",
            view=GuessSelectView(
                self.bot, self.round_id, veil_members, interaction.message,
                cooldown_seconds=config.guess_cooldown_seconds,
            ),
            ephemeral=True,
        )


class SubmitPreviewView(discord.ui.View):
    """Ephemeral preview shown to the submitter; Post publishes to the game channel.

    The round row is only inserted to the DB when Post is clicked, so a timeout
    or dismissal before posting leaves no orphan record.
    """

    def __init__(
        self,
        bot: "Bot",
        crops: list[bytes],
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
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.crops = crops
        self.crop_index = 0
        self.total_rerolls = 0
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

        self.reroll_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label=f"Re-roll (1/{len(crops)})",
            style=discord.ButtonStyle.secondary,
            disabled=len(crops) <= 1,
        )
        self.reroll_btn.callback = self._on_reroll
        self.add_item(self.reroll_btn)

        self.post_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="Post",
            style=discord.ButtonStyle.success,
        )
        self.post_btn.callback = self._on_post
        self.add_item(self.post_btn)

    async def _on_reroll(self, interaction: discord.Interaction) -> None:
        self.crop_index = (self.crop_index + 1) % len(self.crops)
        self.total_rerolls += 1
        self.reroll_btn.label = f"Re-roll ({self.crop_index + 1}/{len(self.crops)})"

        new_crop = self.crops[self.crop_index]
        preview_file = discord.File(io.BytesIO(new_crop), filename="preview.jpg")
        preview_embed = discord.Embed(
            title="Your crop preview",
            description=f"Crop {self.crop_index + 1} of {len(self.crops)} — click Post when happy",
        ).set_image(url="attachment://preview.jpg")
        await interaction.response.edit_message(
            embed=preview_embed,
            attachments=[preview_file],
            view=self,
        )

    async def _on_post(self, interaction: discord.Interaction) -> None:
        assert interaction.guild
        async with self._post_lock:
            if self._posted:
                await interaction.response.send_message(
                    "Already posted.", ephemeral=True
                )
                return
            self._posted = True
            self.reroll_btn.disabled = True
            self.post_btn.disabled = True

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

        if self.total_rerolls:
            await asyncio.to_thread(_do_set_reroll_count, db_path, round_id, self.total_rerolls)

        if self._original_bytes:
            orig_path = _VEIL_ORIG_DIR / f"{round_id}{self._original_ext}"

            def _write_original() -> None:
                _VEIL_ORIG_DIR.mkdir(parents=True, exist_ok=True)
                orig_path.write_bytes(self._original_bytes)

            await asyncio.to_thread(_write_original)
            await asyncio.to_thread(_do_set_original_path, db_path, round_id, str(orig_path))

        crop = self.crops[self.crop_index]
        crop_file = discord.File(io.BytesIO(crop), filename="SPOILER_veil_crop.jpg")
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
            _do_update_round_message,
            db_path,
            round_id,
            game_msg.id,
            crop_url,
            "",
        )

        await asyncio.to_thread(
            _do_audit, db_path,
            guild_id=self.guild_id, actor_id=self._submitter_id,
            action="submit", round_id=round_id,
            details={"difficulty": self._difficulty, "rerolls": self.total_rerolls},
        )

        await interaction.edit_original_response(
            content=f"✅ Posted to {veil_channel.mention}!",
            view=self,
        )


# ── VeilCog ──────────────────────────────────────────────────────────────────

class VeilCog(commands.Cog):
    veil = app_commands.Group(
        name="veil",
        description="Veil guessing game commands.",
        guild_only=True,
    )

    def __init__(self, bot: "Bot") -> None:
        self.bot = bot
        super().__init__()

    async def cog_load(self) -> None:
        """Re-register persistent GameViews for unsolved rounds (capped)."""
        db_path = self.bot.ctx.db_path
        round_ids = await asyncio.to_thread(
            _do_load_unsolved_round_ids, db_path, limit=_COG_LOAD_VIEW_CAP
        )
        for rid in round_ids:
            self.bot.add_view(GameView(self.bot, rid, solved=False))
        log.info("veil: re-registered %d persistent GameViews (cap %d)",
                 len(round_ids), _COG_LOAD_VIEW_CAP)

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

        dim_ok, *_ = await asyncio.to_thread(
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

        if not pipeline_result.crops:
            await interaction.followup.send(
                "Couldn't find a viable crop region — try a different image.", ephemeral=True
            )
            return

        preview_file = discord.File(io.BytesIO(pipeline_result.crops[0]), filename="preview.jpg")
        preview_embed = discord.Embed(
            title="Your crop preview",
            description=f"Crop 1 of {len(pipeline_result.crops)} — click Post when happy",
        ).set_image(url="attachment://preview.jpg")
        original_ext = (Path(image.filename).suffix or ".jpg").lower()
        await interaction.followup.send(
            embed=preview_embed,
            file=preview_file,
            view=SubmitPreviewView(
                self.bot,
                pipeline_result.crops,
                interaction.guild.id,
                config.veil_channel_id,
                submitter_id=interaction.user.id,
                answer_id=interaction.user.id,
                difficulty=config.crop_difficulty,
                candidate_count=len(pipeline_result.candidates),
                veil_role_id=config.veil_role_id,
                original_bytes=image_bytes,
                original_ext=original_ext,
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


async def setup(bot: "Bot") -> None:
    await bot.add_cog(VeilCog(bot))
