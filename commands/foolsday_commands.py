"""April Fools name-shuffle command.

Records current display names, then shuffles nicknames among members who
have been active in at least 3 of the last 5 days.  A restore option sets
everyone back to their original name.  While active, names are reshuffled
every hour via a background loop.
"""
from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import time as _time
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.foolsday")

DAY_SECONDS = 86400
RESHUFFLE_INTERVAL = 3600  # seconds between automatic reshuffles


def _derangement(items: list[str], own: list[str]) -> list[str]:
    """Return a permutation of *items* where result[i] != own[i] for all i.

    *items* is the pool of names to distribute (one per slot).
    *own* is the name each slot must NOT receive (the member's original).
    Both lists must have the same length.  Falls back to a best-effort
    shuffle if a perfect derangement is impossible (e.g. one name makes
    up more than half the pool).
    """
    n = len(items)
    if n < 2:
        return list(items)

    for _ in range(50):
        shuffled = list(items)
        random.shuffle(shuffled)
        if all(shuffled[i] != own[i] for i in range(n)):
            return shuffled

    # Fallback: build a derangement by swapping violations
    shuffled = list(items)
    random.shuffle(shuffled)
    violations = [i for i in range(n) if shuffled[i] == own[i]]
    for i in violations:
        # Find a partner to swap with that fixes both
        swapped = False
        candidates = list(range(n))
        random.shuffle(candidates)
        for j in candidates:
            if j == i:
                continue
            # After swap: shuffled[i] gets shuffled[j], shuffled[j] gets shuffled[i]
            if shuffled[j] != own[i] and shuffled[i] != own[j]:
                shuffled[i], shuffled[j] = shuffled[j], shuffled[i]
                swapped = True
                break
        if not swapped:
            # Best effort: swap with anyone who isn't us
            for j in candidates:
                if j != i and shuffled[j] != own[i]:
                    shuffled[i], shuffled[j] = shuffled[j], shuffled[i]
                    break
    return shuffled


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _init_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS foolsday_names (
            guild_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            original   TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS foolsday_exclusions (
            guild_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )


def _save_names(
    conn: sqlite3.Connection,
    guild_id: int,
    names: dict[int, str],
) -> None:
    """Store original display names, overwriting any previous snapshot."""
    conn.execute("DELETE FROM foolsday_names WHERE guild_id = ?", (guild_id,))
    conn.executemany(
        "INSERT INTO foolsday_names (guild_id, user_id, original) VALUES (?, ?, ?)",
        [(guild_id, uid, name) for uid, name in names.items()],
    )


def _load_names(
    conn: sqlite3.Connection,
    guild_id: int,
) -> dict[int, str]:
    rows = conn.execute(
        "SELECT user_id, original FROM foolsday_names WHERE guild_id = ?",
        (guild_id,),
    ).fetchall()
    return {int(r[0]): r[1] for r in rows}


def _clear_names(conn: sqlite3.Connection, guild_id: int) -> None:
    conn.execute("DELETE FROM foolsday_names WHERE guild_id = ?", (guild_id,))


def _active_user_ids(
    conn: sqlite3.Connection,
    guild_id: int,
    min_days: int = 3,
    window_days: int = 5,
) -> set[int]:
    """Return user IDs that posted on at least *min_days* of the last *window_days*."""
    cutoff = int(_time.time()) - window_days * DAY_SECONDS
    rows = conn.execute(
        """
        SELECT user_id
        FROM (
            SELECT user_id,
                   COUNT(DISTINCT CAST(created_at / ? AS INTEGER)) AS active_days
            FROM processed_messages
            WHERE guild_id = ? AND created_at >= ?
            GROUP BY user_id
        )
        WHERE active_days >= ?
        """,
        (DAY_SECONDS, guild_id, cutoff, min_days),
    ).fetchall()
    return {int(r[0]) for r in rows}


def _excluded_user_ids(conn: sqlite3.Connection, guild_id: int) -> set[int]:
    rows = conn.execute(
        "SELECT user_id FROM foolsday_exclusions WHERE guild_id = ?",
        (guild_id,),
    ).fetchall()
    return {int(r[0]) for r in rows}


# ---------------------------------------------------------------------------
# Reshuffle helper (used by both the command and the background loop)
# ---------------------------------------------------------------------------

async def _reshuffle_guild(guild: discord.Guild, conn: sqlite3.Connection, bot_user_id: int) -> None:
    """Reshuffle nicknames among members that have saved originals."""
    saved = _load_names(conn, guild.id)
    if not saved:
        return

    excluded = _excluded_user_ids(conn, guild.id)
    bot_member = guild.get_member(bot_user_id)
    candidates: list[discord.Member] = []
    for uid in saved:
        if uid in excluded:
            continue
        m = guild.get_member(uid)
        if m is None or m.bot:
            continue
        if m.id == guild.owner_id:
            continue
        if bot_member and m.top_role >= bot_member.top_role:
            continue
        candidates.append(m)

    if len(candidates) < 2:
        log.info("Foolsday reshuffle: not enough renameable members (%d), skipping", len(candidates))
        return

    # 20% chance: everyone gets the same random name from the pool
    own = [saved[m.id] for m in candidates]
    if random.random() < 0.20:
        single = random.choice(own)
        names = [single] * len(candidates)
        log.info("Foolsday reshuffle: same-name mode — everyone becomes %r", single)
    else:
        # Normal 1:1 derangement
        names = _derangement(list(own), own)

    renamed = 0
    skipped = 0
    failed = 0
    for member, new_name in zip(candidates, names):
        if member.display_name == new_name:
            skipped += 1
            continue
        try:
            log.debug("Reshuffle %s (%d): %r -> %r", member, member.id, member.display_name, new_name)
            await member.edit(nick=new_name, reason="April Fools hourly reshuffle")
            renamed += 1
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Reshuffle could not rename %s (%d): %s", member, member.id, exc)
            failed += 1

    log.info("Foolsday reshuffle complete: %d renamed, %d skipped (unchanged), %d failed", renamed, skipped, failed)


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

async def foolsday_loop(bot: discord.Client, db_path: Path) -> None:
    """Reshuffle names every hour while the foolsday shuffle is active."""
    from db_utils import open_db as _open_db

    await bot.wait_until_ready()

    while not bot.is_closed():
        await asyncio.sleep(RESHUFFLE_INTERVAL)
        try:
            with _open_db(db_path) as conn:
                _init_table(conn)
                rows = conn.execute(
                    "SELECT DISTINCT guild_id FROM foolsday_names"
                ).fetchall()
                if not rows:
                    continue
                for (guild_id,) in rows:
                    guild = bot.get_guild(guild_id)
                    if guild is None:
                        continue
                    log.info("Foolsday hourly reshuffle for guild %s (%d)", guild.name, guild.id)
                    await _reshuffle_guild(guild, conn, bot.user.id if bot.user else 0)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Foolsday reshuffle loop iteration failed")


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

def register_foolsday_commands(bot: "Bot", ctx: "AppContext") -> None:

    @bot.tree.command(
        name="foolsday",
        description="April Fools name shuffle — randomise or restore member nicknames.",
    )
    @app_commands.describe(
        action="shuffle = randomise names, restore = set names back to original.",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="shuffle", value="shuffle"),
        app_commands.Choice(name="restore", value="restore"),
    ])
    async def foolsday(
        interaction: discord.Interaction,
        action: str,
    ) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        with ctx.open_db() as conn:
            _init_table(conn)

            if action == "shuffle":
                # Find active members
                active_ids = _active_user_ids(conn, guild.id)
                log.info("Foolsday shuffle initiated by %s — %d active user(s) found", interaction.user, len(active_ids))
                if len(active_ids) < 2:
                    await interaction.followup.send(
                        "Not enough active members to shuffle (need at least 2).",
                        ephemeral=True,
                    )
                    return

                # Resolve to guild members the bot can rename
                excluded = _excluded_user_ids(conn, guild.id)
                bot_member = guild.get_member(bot.user.id) if bot.user else None
                candidates: list[discord.Member] = []
                skipped_bot = 0
                skipped_owner = False
                skipped_role = 0
                skipped_excluded = 0
                for uid in active_ids:
                    if uid in excluded:
                        skipped_excluded += 1
                        continue
                    m = guild.get_member(uid)
                    if m is None or m.bot:
                        skipped_bot += 1
                        continue
                    if m.id == guild.owner_id:
                        skipped_owner = True
                        continue
                    if bot_member and m.top_role >= bot_member.top_role:
                        skipped_role += 1
                        continue
                    candidates.append(m)

                log.info(
                    "Foolsday candidates: %d renameable, %d excluded, %d bots/missing, %d above bot role, owner skipped=%s",
                    len(candidates), skipped_excluded, skipped_bot, skipped_role, skipped_owner,
                )

                if len(candidates) < 2:
                    await interaction.followup.send(
                        "Not enough renameable active members (need at least 2). "
                        "Members above my role or the server owner can't be renamed.",
                        ephemeral=True,
                    )
                    return

                # Save originals
                originals = {m.id: m.display_name for m in candidates}
                _save_names(conn, guild.id, originals)

                # Shuffle names — 1:1 derangement so nobody keeps their own
                own = [originals[m.id] for m in candidates]
                names = _derangement(list(own), own)

                # Apply
                renamed = 0
                failed = 0
                for member, new_name in zip(candidates, names):
                    try:
                        log.debug("Renaming %s (%d): %r -> %r", member, member.id, member.display_name, new_name)
                        await member.edit(nick=new_name, reason="April Fools name shuffle")
                        renamed += 1
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        log.warning("Could not rename %s (%d): %s", member, member.id, exc)
                        failed += 1

                log.info("Foolsday shuffle complete: %d renamed, %d failed", renamed, failed)
                msg = f"Shuffled **{renamed}** member nicknames."
                if failed:
                    msg += f"\nFailed to rename **{failed}** members (permission issues)."
                msg += "\nNames will reshuffle automatically every hour."
                msg += "\nUse `/foolsday action:restore` to stop and undo."
                await interaction.followup.send(msg, ephemeral=True)

            else:
                # Restore
                saved = _load_names(conn, guild.id)
                log.info("Foolsday restore initiated by %s — %d saved name(s)", interaction.user, len(saved))
                if not saved:
                    await interaction.followup.send(
                        "No saved names found — nothing to restore.",
                        ephemeral=True,
                    )
                    return

                restored = 0
                failed = 0
                for uid, original_name in saved.items():
                    m = guild.get_member(uid)
                    if m is None:
                        continue
                    # Set nick to None if original matches their username
                    # (i.e. they had no nickname before)
                    nick = None if original_name == m.name else original_name
                    try:
                        log.debug("Restoring %s (%d): %r -> %r", m, m.id, m.display_name, original_name)
                        await m.edit(nick=nick, reason="April Fools restore")
                        restored += 1
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        log.warning("Could not restore %s (%d): %s", m, m.id, exc)
                        failed += 1

                log.info("Foolsday restore complete: %d restored, %d failed", restored, failed)
                _clear_names(conn, guild.id)

                msg = f"Restored **{restored}** member nicknames."
                if failed:
                    msg += f"\nFailed to restore **{failed}** members."
                await interaction.followup.send(msg, ephemeral=True)

    @bot.tree.command(
        name="foolsday_exclude",
        description="Opt out of the April Fools name shuffle (mods can exclude others).",
    )
    @app_commands.describe(user="(Mod only) The member to exclude. Leave blank to exclude yourself.")
    async def foolsday_exclude(interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        # Non-mods can only exclude themselves
        if user is not None and user.id != interaction.user.id and not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You can only exclude yourself. Run `/foolsday_exclude` with no user to opt out.",
                ephemeral=True,
            )
            return

        target = user or (interaction.guild.get_member(interaction.user.id) if interaction.guild else None)
        if target is None:
            await interaction.response.send_message("Could not resolve member.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        details: list[str] = []
        with ctx.open_db() as conn:
            _init_table(conn)
            conn.execute(
                "INSERT OR IGNORE INTO foolsday_exclusions (guild_id, user_id) VALUES (?, ?)",
                (guild.id, target.id),
            )

            # If a shuffle is active, fix names immediately
            saved = _load_names(conn, guild.id)
            original_name = saved.get(target.id)
            if original_name is not None:
                current_nick = target.display_name

                # Restore the excluded user to their original name
                nick = None if original_name == target.name else original_name
                try:
                    log.info("Foolsday exclude: restoring %s (%d) to %r", target, target.id, original_name)
                    await target.edit(nick=nick, reason="April Fools — excluded, restoring original name")
                    details.append(f"Restored {target.mention} to their original name.")
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("Foolsday exclude: could not restore %s (%d): %s", target, target.id, exc)
                    details.append(f"Could not restore {target.mention}'s name: {exc}")

                # Find whoever is currently wearing the excluded user's original name
                # and give them the name the excluded user was wearing
                for uid, orig in saved.items():
                    if uid == target.id:
                        continue
                    m = guild.get_member(uid)
                    if m is None:
                        continue
                    if m.display_name == original_name:
                        try:
                            log.info(
                                "Foolsday exclude: reassigning %s (%d) from %r to %r",
                                m, m.id, m.display_name, current_nick,
                            )
                            await m.edit(nick=current_nick, reason="April Fools — reassigned after exclusion")
                            details.append(f"Reassigned {m.mention} to a different name.")
                        except (discord.Forbidden, discord.HTTPException) as exc:
                            log.warning("Foolsday exclude: could not reassign %s (%d): %s", m, m.id, exc)
                            details.append(f"Could not reassign {m.mention}: {exc}")
                        break

                # Remove the excluded user from saved names so reshuffles skip them
                conn.execute(
                    "DELETE FROM foolsday_names WHERE guild_id = ? AND user_id = ?",
                    (guild.id, target.id),
                )

        log.info("Foolsday: %s excluded %s (%d)", interaction.user, target, target.id)
        msg = f"{target.mention} excluded from foolsday shuffle."
        if details:
            msg += "\n" + "\n".join(details)
        await interaction.followup.send(msg, ephemeral=True)

    @bot.tree.command(
        name="foolsday_join",
        description="Join the active April Fools name shuffle (mods can add others).",
    )
    @app_commands.describe(user="(Mod only) The member to add. Leave blank to join yourself.")
    async def foolsday_join(interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        if user is not None and user.id != interaction.user.id and not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You can only add yourself. Run `/foolsday_join` with no user to opt in.",
                ephemeral=True,
            )
            return

        target = user or (interaction.guild.get_member(interaction.user.id) if interaction.guild else None)
        if target is None:
            await interaction.response.send_message("Could not resolve member.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        with ctx.open_db() as conn:
            _init_table(conn)
            saved = _load_names(conn, guild.id)

            if not saved:
                await interaction.followup.send(
                    "No shuffle is currently active. A mod needs to run `/foolsday action:shuffle` first.",
                    ephemeral=True,
                )
                return

            original_for_target = saved.get(target.id)
            if original_for_target is not None and target.display_name != original_for_target:
                # Already in the shuffle and actually renamed
                await interaction.followup.send(
                    f"{target.mention} is already in the shuffle.", ephemeral=True,
                )
                return

            # Remove from exclusion list if present
            conn.execute(
                "DELETE FROM foolsday_exclusions WHERE guild_id = ? AND user_id = ?",
                (guild.id, target.id),
            )

            # Pick a random current participant to swap with
            excluded = _excluded_user_ids(conn, guild.id)
            swap_candidates: list[discord.Member] = []
            for uid in saved:
                if uid in excluded:
                    continue
                m = guild.get_member(uid)
                if m is not None and not m.bot:
                    swap_candidates.append(m)

            if not swap_candidates:
                await interaction.followup.send(
                    "No active participants to swap with.", ephemeral=True,
                )
                return

            partner = random.choice(swap_candidates)
            # Use the already-saved original if they were in the table but never renamed
            target_original = original_for_target or target.display_name
            partner_current = partner.display_name

            # Save the new user's original name (no-op if already saved)
            conn.execute(
                "INSERT OR REPLACE INTO foolsday_names (guild_id, user_id, original) VALUES (?, ?, ?)",
                (guild.id, target.id, target_original),
            )

            # Swap: target gets partner's current nick, partner gets target's original
            details: list[str] = []
            try:
                log.debug("Foolsday join: %s (%d) %r -> %r", target, target.id, target_original, partner_current)
                await target.edit(nick=partner_current, reason="April Fools — joined shuffle")
                details.append(f"{target.mention} joined the shuffle.")
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Foolsday join: could not rename %s (%d): %s", target, target.id, exc)
                details.append(f"Could not rename {target.mention}: {exc}")

            try:
                log.debug("Foolsday join: swapping %s (%d) %r -> %r", partner, partner.id, partner_current, target_original)
                await partner.edit(nick=target_original, reason="April Fools — swapped after new join")
                details.append(f"Swapped {partner.mention} to a new name.")
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Foolsday join: could not rename %s (%d): %s", partner, partner.id, exc)
                details.append(f"Could not rename {partner.mention}: {exc}")

            log.info("Foolsday: %s added %s (%d) to shuffle, swapped with %s (%d)",
                     interaction.user, target, target.id, partner, partner.id)

        await interaction.followup.send("\n".join(details), ephemeral=True)

    @bot.tree.command(
        name="foolsday_include",
        description="Remove a user from the April Fools exclusion list.",
    )
    @app_commands.describe(user="The member to include again.")
    async def foolsday_include(interaction: discord.Interaction, user: discord.Member) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        with ctx.open_db() as conn:
            _init_table(conn)
            conn.execute(
                "DELETE FROM foolsday_exclusions WHERE guild_id = ? AND user_id = ?",
                (guild.id, user.id),
            )
        log.info("Foolsday: %s re-included %s (%d)", interaction.user, user, user.id)
        await interaction.response.send_message(f"{user.mention} is no longer excluded from foolsday shuffle.", ephemeral=True)

    @bot.tree.command(
        name="foolsday_exclusions",
        description="List users excluded from the April Fools name shuffle.",
    )
    async def foolsday_exclusions(interaction: discord.Interaction) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        with ctx.open_db() as conn:
            _init_table(conn)
            excluded = _excluded_user_ids(conn, guild.id)

        if not excluded:
            await interaction.response.send_message("No users are excluded.", ephemeral=True)
            return

        lines: list[str] = []
        for uid in excluded:
            m = guild.get_member(uid)
            lines.append(f"- {m.mention}" if m else f"- Unknown user ({uid})")
        await interaction.response.send_message(
            f"**Foolsday exclusions ({len(lines)}):**\n" + "\n".join(lines),
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # Repair tool
    # ------------------------------------------------------------------

    class _RepairSelect(discord.ui.Select["_RepairView"]):
        def __init__(self, options: list[discord.SelectOption], repair_view: "_RepairView"):
            super().__init__(placeholder="Pick the user this name belongs to…", options=options)
            self.repair_view = repair_view

        async def callback(self, interaction: discord.Interaction) -> None:
            await self.repair_view.handle_select(interaction, self.values[0])

    class _RepairPageButton(discord.ui.Button["_RepairView"]):
        def __init__(self, label: str, repair_view: "_RepairView", direction: int, *, disabled: bool = False):
            super().__init__(label=label, style=discord.ButtonStyle.secondary, disabled=disabled)
            self.repair_view = repair_view
            self.direction = direction

        async def callback(self, interaction: discord.Interaction) -> None:
            await self.repair_view.handle_page(interaction, self.direction)

    class _RepairSkipButton(discord.ui.Button["_RepairView"]):
        def __init__(self, repair_view: "_RepairView"):
            super().__init__(label="Skip", style=discord.ButtonStyle.danger)
            self.repair_view = repair_view

        async def callback(self, interaction: discord.Interaction) -> None:
            await self.repair_view.handle_skip(interaction)

    class _RepairView(discord.ui.View):
        """Walk through current display names of active members and let a mod
        assign each to the correct user so the DB can be rebuilt.
        Uses a string Select with real usernames instead of UserSelect
        (which would show shuffled nicknames)."""

        MAX_OPTIONS = 25  # Discord select menu limit

        def __init__(
            self,
            guild: discord.Guild,
            names: list[str],
            members: list[discord.Member],
            open_db_fn,
        ):
            super().__init__(timeout=600)
            self.guild = guild
            self.names = names
            # members sorted by username for the dropdown
            self.members = sorted(members, key=lambda m: m.name.lower())
            self.member_map = {str(m.id): m for m in self.members}
            self.open_db_fn = open_db_fn
            self.index = 0
            self.assignments: dict[str, int] = {}  # original_name -> user_id
            self.skipped: list[str] = []
            self.restored = 0
            self.failed = 0
            self.page = 0  # for paginating the dropdown when > 25 members
            self._rebuild_select()

        @property
        def _available_members(self) -> list[discord.Member]:
            """Members not yet assigned."""
            assigned = set(self.assignments.values())
            return [m for m in self.members if m.id not in assigned]

        @property
        def _total_pages(self) -> int:
            avail = len(self._available_members)
            return max(1, (avail + self.MAX_OPTIONS - 1) // self.MAX_OPTIONS)

        def _rebuild_select(self) -> None:
            self.clear_items()
            available = self._available_members
            start = self.page * self.MAX_OPTIONS
            page_members = available[start : start + self.MAX_OPTIONS]

            options = [
                discord.SelectOption(label=m.name, value=str(m.id))
                for m in page_members
            ] or [discord.SelectOption(label="—", value="none")]

            self.add_item(_RepairSelect(options, self))
            self.add_item(_RepairSkipButton(self))

            # Pagination buttons if needed
            if self._total_pages > 1:
                self.add_item(_RepairPageButton("◀ Prev", self, -1, disabled=self.page == 0))
                self.add_item(_RepairPageButton("Next ▶", self, 1, disabled=self.page >= self._total_pages - 1))

        def _prompt(self) -> str:
            page_info = f" (page {self.page + 1}/{self._total_pages})" if self._total_pages > 1 else ""
            return (
                f"**Name {self.index + 1} of {len(self.names)}**\n"
                f"# `{self.names[self.index]}`\n"
                f"Select the user whose **real** display name this is.{page_info}"
            )

        def _summary(self) -> str:
            lines = ["**Repair complete — updated mappings:**"]
            for name, uid in self.assignments.items():
                lines.append(f"- `{name}` → <@{uid}>")
            if self.skipped:
                lines.append(f"\n**Skipped ({len(self.skipped)}):** " + ", ".join(f"`{n}`" for n in self.skipped))
            return "\n".join(lines)

        async def handle_select(self, interaction: discord.Interaction, value: str) -> None:
            if value == "none":
                return
            chosen = self.member_map.get(value)
            if chosen is None:
                await interaction.response.edit_message(content="User not found.\n\n" + self._prompt(), view=self)
                return

            name = self.names[self.index]

            if chosen.id in self.assignments.values():
                already = next(n for n, uid in self.assignments.items() if uid == chosen.id)
                await interaction.response.edit_message(
                    content=(
                        f"**{chosen.name}** was already assigned to `{already}`.\n"
                        f"Pick someone else for `{name}`.\n\n" + self._prompt()
                    ),
                    view=self,
                )
                return

            self.assignments[name] = chosen.id
            log.info("Foolsday repair: %r assigned to %s (%d)", name, chosen, chosen.id)

            # Restore immediately
            member = self.guild.get_member(chosen.id)
            if member is not None:
                nick = None if name == member.name else name
                try:
                    await member.edit(nick=nick, reason="April Fools repair — restoring original name")
                    self.restored += 1
                    log.info("Foolsday repair: restored %s (%d) to %r", member, member.id, name)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    self.failed += 1
                    log.warning("Foolsday repair: could not restore %s (%d): %s", member, member.id, exc)

            # Update DB immediately
            with self.open_db_fn() as conn:
                _init_table(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO foolsday_names (guild_id, user_id, original) VALUES (?, ?, ?)",
                    (self.guild.id, chosen.id, name),
                )

            await self._advance(interaction)

        async def handle_skip(self, interaction: discord.Interaction) -> None:
            name = self.names[self.index]
            self.skipped.append(name)
            log.info("Foolsday repair: skipped %r", name)
            await self._advance(interaction)

        async def _advance(self, interaction: discord.Interaction) -> None:
            self.index += 1
            if self.index < len(self.names):
                self.page = 0
                self._rebuild_select()
                await interaction.response.edit_message(content=self._prompt(), view=self)
            else:
                self.stop()
                await interaction.response.edit_message(content="Applying fixes…", view=None)
                await self._apply(interaction)

        async def handle_page(self, interaction: discord.Interaction, direction: int) -> None:
            self.page = max(0, min(self._total_pages - 1, self.page + direction))
            self._rebuild_select()
            await interaction.response.edit_message(content=self._prompt(), view=self)

        async def _apply(self, interaction: discord.Interaction) -> None:
            log.info("Foolsday repair complete: %d restored, %d failed, %d skipped",
                     self.restored, self.failed, len(self.skipped))
            summary = self._summary()
            summary += f"\n\nRestored **{self.restored}** nicknames."
            if self.failed:
                summary += f" Failed **{self.failed}**."
            summary += "\nUse `/foolsday action:restore` to stop the shuffle, or wait for the next hourly reshuffle."
            await interaction.edit_original_response(content=summary)

        async def on_timeout(self) -> None:
            log.info("Foolsday repair timed out at name %d/%d", self.index + 1, len(self.names))

    @bot.tree.command(
        name="foolsday_samename",
        description="Set everyone in the shuffle to the same random name (or a custom one).",
    )
    @app_commands.describe(name="Custom name to use. Leave blank for a random name from the pool.")
    async def foolsday_samename(interaction: discord.Interaction, name: str | None = None) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True,
            )
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        with ctx.open_db() as conn:
            _init_table(conn)
            saved = _load_names(conn, guild.id)

        if not saved:
            await interaction.followup.send(
                "No shuffle is currently active.", ephemeral=True,
            )
            return

        with ctx.open_db() as conn:
            excluded = _excluded_user_ids(conn, guild.id)

        # Pick the name
        if name is None:
            pool = [n for uid, n in saved.items() if uid not in excluded]
            name = random.choice(pool) if pool else random.choice(list(saved.values()))

        bot_member = guild.get_member(bot.user.id) if bot.user else None
        renamed = 0
        failed = 0
        for uid in saved:
            if uid in excluded:
                continue
            m = guild.get_member(uid)
            if m is None or m.bot:
                continue
            if m.id == guild.owner_id:
                continue
            if bot_member and m.top_role >= bot_member.top_role:
                continue
            if m.display_name == name:
                continue
            try:
                await m.edit(nick=name, reason="April Fools — same name mode")
                renamed += 1
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Foolsday samename: could not rename %s (%d): %s", m, m.id, exc)
                failed += 1

        log.info("Foolsday samename: set %d members to %r (%d failed), initiated by %s",
                 renamed, name, failed, interaction.user)

        msg = f"Set **{renamed}** members to `{name}`."
        if failed:
            msg += f" Failed **{failed}**."
        msg += "\nThe next hourly reshuffle will return to normal shuffling."
        await interaction.followup.send(msg, ephemeral=True)

    @bot.tree.command(
        name="foolsday_repair",
        description="Restore original nicknames for any member the bot has renamed.",
    )
    async def foolsday_repair(interaction: discord.Interaction) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True,
            )
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        # Gather all renameable members (no activity filter)
        bot_member = guild.get_member(bot.user.id) if bot.user else None
        bot_user_id = bot.user.id if bot.user else 0
        members: list[discord.Member] = []
        for m in guild.members:
            if m.bot:
                continue
            if m.id == guild.owner_id:
                continue
            if bot_member and m.top_role >= bot_member.top_role:
                continue
            members.append(m)

        if not members:
            await interaction.response.send_message(
                "No renameable members found.", ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # --- Audit log phase: find original nick before the bot first changed it ---
        # Walk the audit log for member_update actions performed by the bot.
        # For each member, collect every nick change the bot made, ordered oldest
        # first.  The `before.nick` of the earliest entry is the pre-shuffle name.
        member_ids = {m.id for m in members}
        # {user_id: (before_nick, entry_created_at)} — keep only the oldest per user
        oldest_change: dict[int, tuple[str | None, float]] = {}

        try:
            async for entry in guild.audit_logs(
                action=discord.AuditLogAction.member_update,
                limit=None,
            ):
                if entry.user is None or entry.user.id != bot_user_id:
                    continue
                target = entry.target
                if target is None:
                    continue
                target_id: int = target if isinstance(target, int) else target.id  # type: ignore[assignment]
                if target_id not in member_ids:
                    continue
                # Only care about nick changes
                before_nick = getattr(entry.before, "nick", discord.utils.MISSING)
                if before_nick is discord.utils.MISSING:
                    continue
                uid = target_id
                ts = entry.created_at.timestamp()
                if uid not in oldest_change or ts < oldest_change[uid][1]:
                    oldest_change[uid] = (before_nick, ts)
        except discord.Forbidden:
            await interaction.followup.send(
                "I need the **View Audit Log** permission to look up original names.",
                ephemeral=True,
            )
            return

        if not oldest_change:
            await interaction.followup.send(
                "No bot nickname changes found in the audit log. "
                "Either the log has been pruned or no shuffle has happened.",
                ephemeral=True,
            )
            return

        # --- Restore phase ---
        restored = 0
        failed = 0
        skipped = 0
        lines: list[str] = []

        for m in members:
            if m.id not in oldest_change:
                skipped += 1
                continue

            original_nick = oldest_change[m.id][0]  # None means they had no nickname
            # If the member already has this name, skip
            current_nick = m.nick
            if current_nick == original_nick:
                skipped += 1
                lines.append(f"- **{m.name}** — already correct")
                continue

            try:
                await m.edit(nick=original_nick, reason="April Fools repair — audit log restore")
                restored += 1
                display_orig = original_nick or f"(no nickname / {m.name})"
                lines.append(f"- **{m.name}** → `{display_orig}`")
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Foolsday repair: could not restore %s (%d): %s", m, m.id, exc)
                failed += 1
                lines.append(f"- **{m.name}** — failed: {exc}")

        # Clear saved names so the hourly reshuffle loop stops.
        with ctx.open_db() as conn:
            _init_table(conn)
            _clear_names(conn, guild.id)

        log.info("Foolsday repair (audit log): %d restored, %d failed, %d skipped, %d not in audit log",
                 restored, failed, skipped, len(member_ids) - len(oldest_change))

        summary = (
            f"**Audit Log Repair Complete**\n"
            f"Restored: {restored} · Failed: {failed} · Skipped: {skipped} · "
            f"Not in audit log: {len(member_ids) - len(oldest_change)}\n"
            f"----------------------------------\n"
        )
        if lines:
            summary += "\n".join(lines)

        await _send_chunked_ephemeral(interaction, summary)


    async def _send_chunked_ephemeral(interaction: discord.Interaction, text: str) -> None:
        """Send a long ephemeral followup, splitting on newlines if needed."""
        while text:
            if len(text) <= 1900:
                await interaction.followup.send(text, ephemeral=True)
                break
            split = text.rfind("\n", 0, 1900)
            if split <= 0:
                split = 1900
            await interaction.followup.send(text[:split], ephemeral=True)
            text = text[split:].lstrip("\n")
