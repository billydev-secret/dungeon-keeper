"""QA Tracker — the verdict-button surface on testing-queue cards.

Member self-service only: three restart-safe ``DynamicItem`` buttons ride
every QA card (Pass / Fail / Blocked), gated on the configured QA-crew
role. Verdicts land through ``qa_service.record_verdict`` (instant pay,
guild-local daily cap) and the card embed re-renders in place; fail and
blocked notes collect in a lazily-created thread on the card. All admin
knobs live on the web dashboard (stage 3) — this cog ships no commands.

Dynamic items dispatch purely on ``custom_id``, so buttons on cards the
stage-2 hook posts over raw REST work identically to bot-posted ones.
See docs/plans/qa-tracker.md.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, cast

import discord
from discord.ext import commands

from bot_modules.core.db_utils import get_tz_offset_hours, open_db
from bot_modules.economy.logic import local_day_for
from bot_modules.qa.cards import VERDICT_EMOJI, build_card_embed
from bot_modules.services.qa_service import (
    QASettings,
    archive_test,
    get_test,
    list_stale_passed,
    list_verdicts,
    load_qa_settings,
    record_verdict,
    set_test_thread,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.qa")

_DISABLED_MSG = "The QA tracker is disabled on this server."
_NO_ROLE_MSG = "You need the QA-crew role to record verdicts."
_ARCHIVED_MSG = "This test is archived — verdicts are closed."
_GUILD_ONLY_MSG = "This only works in a server."

_VERDICT_LABELS = {"fail": "Failed", "blocked": "Blocked"}

# A passed card is decluttered from the channel once it's stayed verified this
# long, and the sweep re-checks at this cadence. See qa_archive_sweep_loop.
ARCHIVE_SWEEP_DELAY = timedelta(minutes=10)
ARCHIVE_SWEEP_INTERVAL_SECONDS = 60


def _can_vote(member: discord.Member, settings: QASettings) -> bool:
    """Admins always; otherwise the configured QA-crew role (0 = admins only)."""
    if member.guild_permissions.administrator:
        return True
    role_id = settings.role_id
    return role_id != 0 and any(r.id == role_id for r in member.roles)


def _load_settings(db_path: Path, guild_id: int) -> QASettings:
    with open_db(db_path) as conn:
        return load_qa_settings(conn, guild_id)


def _record(
    db_path: Path,
    settings: QASettings,
    guild_id: int,
    user_id: int,
    test_id: int,
    verdict: str,
    note: str | None,
):
    """Record the verdict and return (outcome, test dict, verdict dicts).

    ``local_day``/``tz_offset`` mirror the economy call sites: the guild's
    configured offset folds "today" for the daily payout cap. Rows are
    converted to plain dicts here so the pure card renderer never sees
    sqlite3.Row objects.
    """
    with open_db(db_path) as conn:
        offset = get_tz_offset_hours(conn, guild_id)
        day = local_day_for(time.time(), offset)
        outcome = record_verdict(
            conn,
            settings,
            test_id,
            guild_id,
            user_id,
            verdict,
            note,
            local_day=day,
            tz_offset=offset,
        )
        test_row = get_test(conn, test_id)
        test = dict(test_row) if test_row is not None else None
        verdicts = [dict(r) for r in list_verdicts(conn, test_id)]
        return outcome, test, verdicts


async def _ensure_thread(
    interaction: discord.Interaction,
    message: discord.Message | None,
    test: dict,
) -> discord.Thread | None:
    """Return the card's notes thread, creating and storing it on first use."""
    guild = interaction.guild
    assert guild is not None  # callers gate on guild
    thread_id = int(test.get("thread_id") or 0)
    if thread_id:
        thread = guild.get_thread(thread_id)
        if thread is None:
            try:
                fetched = await guild.fetch_channel(thread_id)
            except discord.HTTPException:
                fetched = None
            thread = fetched if isinstance(fetched, discord.Thread) else None
        if thread is not None:
            return thread
    if message is None:
        return None
    name = str(test.get("title") or "QA notes")[:100]
    try:
        thread = await message.create_thread(name=name, auto_archive_duration=10080)
    except discord.HTTPException as exc:
        log.warning("qa: failed to create notes thread on test %s: %s", test["id"], exc)
        return None

    ctx = cast("Bot", interaction.client).ctx

    def _store() -> None:
        with open_db(ctx.db_path) as conn:
            set_test_thread(conn, int(test["id"]), thread.id)

    await asyncio.to_thread(_store)
    return thread


async def _finish_verdict(
    interaction: discord.Interaction,
    message: discord.Message | None,
    test_id: int,
    verdict: str,
    note: str | None,
    settings: QASettings,
) -> None:
    """Record + pay, confirm ephemerally, post the note, re-render the card.

    Callers have already gated on guild presence, the QA role, and the
    enabled flag; ``settings`` is the snapshot they gated with.
    """
    guild = interaction.guild
    user = interaction.user
    assert guild is not None

    try:
        outcome, test, verdicts = await asyncio.to_thread(
            _record, cast("Bot", interaction.client).ctx.db_path,
            settings, guild.id, user.id, test_id, verdict, note,
        )
    except ValueError:
        # Archived (or deleted) while the card was still showing buttons.
        await interaction.response.send_message(_ARCHIVED_MSG, ephemeral=True)
        return

    if outcome.paid > 0:
        confirmation = f"Recorded ✅ — +{outcome.paid} 🪙"
    elif outcome.fresh:
        confirmation = "Recorded — daily cap reached, no payout."
    else:
        confirmation = "Updated your verdict."
    await interaction.response.send_message(confirmation, ephemeral=True)

    if test is None:
        return

    # Fail/blocked detail lives with the test, in a thread on the card.
    if note and verdict in _VERDICT_LABELS:
        thread = await _ensure_thread(interaction, message, test)
        if thread is not None:
            try:
                await thread.send(
                    f"{VERDICT_EMOJI[verdict]} **{_VERDICT_LABELS[verdict]}** — "
                    f"{user.mention}: {note}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as exc:
                log.warning("qa: failed to post note on test %s: %s", test_id, exc)

    # Re-render in place; the buttons stay as they are (edit omits the view).
    if message is not None:
        embed = discord.Embed.from_dict(build_card_embed(test, verdicts))
        try:
            await message.edit(embed=embed)
        except discord.HTTPException as exc:
            log.warning("qa: failed to re-render card for test %s: %s", test_id, exc)


class _QANoteModal(discord.ui.Modal):
    """Note prompt for fail (required) and blocked (optional) verdicts."""

    def __init__(
        self, test_id: int, verdict: str, message: discord.Message | None
    ) -> None:
        super().__init__(
            title="What went wrong?" if verdict == "fail" else "What's in the way?"
        )
        self.test_id = test_id
        self.verdict = verdict
        self.message = message
        self.note: discord.ui.TextInput = discord.ui.TextInput(
            label="What went wrong?",
            style=discord.TextStyle.paragraph,
            required=(verdict == "fail"),
            max_length=1000,
            placeholder="What you tried and what you saw.",
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            await interaction.response.send_message(_GUILD_ONLY_MSG, ephemeral=True)
            return
        ctx = cast("Bot", interaction.client).ctx
        # Re-gate: settings can flip between the click and the submit.
        settings = await asyncio.to_thread(_load_settings, ctx.db_path, guild.id)
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return
        if not _can_vote(user, settings):
            await interaction.response.send_message(_NO_ROLE_MSG, ephemeral=True)
            return
        note = str(self.note.value or "").strip() or None
        await _finish_verdict(
            interaction, self.message, self.test_id, self.verdict, note, settings
        )


async def _handle_click(
    interaction: discord.Interaction, test_id: int, verdict: str
) -> None:
    guild = interaction.guild
    user = interaction.user
    if guild is None or not isinstance(user, discord.Member):
        await interaction.response.send_message(_GUILD_ONLY_MSG, ephemeral=True)
        return
    ctx = cast("Bot", interaction.client).ctx
    settings = await asyncio.to_thread(_load_settings, ctx.db_path, guild.id)
    if not settings.enabled:
        await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
        return
    if not _can_vote(user, settings):
        await interaction.response.send_message(_NO_ROLE_MSG, ephemeral=True)
        return

    if verdict == "pass":
        await _finish_verdict(
            interaction, interaction.message, test_id, "pass", None, settings
        )
    else:
        await interaction.response.send_modal(
            _QANoteModal(test_id, verdict, interaction.message)
        )


class _QAPassButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"qa:v:(?P<test_id>[0-9]+):pass",
):
    def __init__(self, test_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Passed",
                emoji="✅",
                style=discord.ButtonStyle.success,
                custom_id=f"qa:v:{test_id}:pass",
            )
        )
        self.test_id = test_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> _QAPassButton:
        return cls(int(match["test_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_click(interaction, self.test_id, "pass")


class _QAFailButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"qa:v:(?P<test_id>[0-9]+):fail",
):
    def __init__(self, test_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Failed",
                emoji="❌",
                style=discord.ButtonStyle.danger,
                custom_id=f"qa:v:{test_id}:fail",
            )
        )
        self.test_id = test_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> _QAFailButton:
        return cls(int(match["test_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_click(interaction, self.test_id, "fail")


class _QABlockedButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"qa:v:(?P<test_id>[0-9]+):blocked",
):
    def __init__(self, test_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Blocked",
                emoji="🚧",
                style=discord.ButtonStyle.secondary,
                custom_id=f"qa:v:{test_id}:blocked",
            )
        )
        self.test_id = test_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> _QABlockedButton:
        return cls(int(match["test_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_click(interaction, self.test_id, "blocked")


async def _sweep_stale_card(bot: Bot, db_path: Path, test: dict) -> None:
    """Delete one long-verified card and mark its test archived.

    Best-effort on the Discord side: a channel the bot can no longer see, or
    a message someone already deleted by hand, still gets archived — there's
    nothing left to clean up. A transient failure (rate limit, hiccup) is
    logged and the row is left 'passed' so the next sweep retries it.
    """
    test_id = int(test["id"])
    channel = bot.get_channel(int(test["channel_id"]))
    if isinstance(channel, discord.abc.Messageable):
        try:
            message = await channel.fetch_message(int(test["message_id"]))
            await message.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException as exc:
            log.warning("qa: failed to delete stale card for test %s: %s", test_id, exc)
            return

    def _archive() -> None:
        with open_db(db_path) as conn:
            archive_test(conn, test_id)

    await asyncio.to_thread(_archive)


async def qa_archive_sweep_loop(bot: Bot) -> None:
    """Delete cards that have sat verified for ``ARCHIVE_SWEEP_DELAY``.

    Registered as a bot startup task; guild-agnostic, matching the
    ``scheduled_games_loop`` polling pattern. Declutters the testing channel
    without touching the audit trail — verdicts and payouts stay in the DB.
    """
    await bot.wait_until_ready()
    db_path = bot.ctx.db_path

    while not bot.is_closed():
        try:
            cutoff = (datetime.now(timezone.utc) - ARCHIVE_SWEEP_DELAY).isoformat()

            def _load() -> list[dict]:
                with open_db(db_path) as conn:
                    return [dict(r) for r in list_stale_passed(conn, cutoff)]

            for test in await asyncio.to_thread(_load):
                try:
                    await _sweep_stale_card(bot, db_path, test)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("qa: sweep failed for test %s", test.get("id"))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("qa_archive_sweep_loop iteration error")
        await asyncio.sleep(ARCHIVE_SWEEP_INTERVAL_SECONDS)


class QACog(commands.Cog):
    """No commands — just the dynamic-item registration for the card buttons."""

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(_QAPassButton, _QAFailButton, _QABlockedButton)
        self.bot.startup_task_factories.append(lambda: qa_archive_sweep_loop(self.bot))


async def setup(bot: Bot) -> None:
    await bot.add_cog(QACog(bot, bot.ctx))
