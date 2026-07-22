"""Views and modals for the Golden Meadow casino — Discord glue only.

Two persistence styles, per the house rules: the hub panel is a static
custom_id view (state-free buttons, one instance registered at cog_load);
blackjack hands and roulette rounds use DynamicItems whose custom_ids carry
the hand/round id, so clicks route after a restart with no re-registration
per message. Every handler lives on the cog; views just parse and dispatch.
"""

from __future__ import annotations

import re

from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from bot_modules.cogs.casino.cog import CasinoCog


def _cog(interaction: discord.Interaction) -> CasinoCog | None:
    cog = interaction.client.get_cog("CasinoCog")  # type: ignore[attr-defined]
    return cog


async def safe_ephemeral(interaction: discord.Interaction, message: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


async def _dispatch_or_apologize(interaction: discord.Interaction) -> CasinoCog | None:
    cog = _cog(interaction)
    if cog is None:
        await safe_ephemeral(interaction, "❌ The casino isn't running right now.")
    return cog


def parse_amount(raw: str) -> int | None:
    """A bet amount from modal text — a positive whole number or None."""
    try:
        value = int(raw.strip().replace(",", ""))
    except ValueError:
        return None
    return value if value > 0 else None


# ── bet modals ─────────────────────────────────────────────────────────


class BetModal(discord.ui.Modal):
    """One amount box; ``game`` (+ coinflip's ``side``) decides the table.

    The label carries the live limits ("Your bet (5–100 · 340 left today)")
    and the box pre-fills the member's last bet on this game — nobody
    should learn about a limit from the error after submitting.
    """

    def __init__(
        self,
        *,
        title: str,
        game: str,
        side: str | None = None,
        limits_label: str = "Your bet",
        default_amount: int | None = None,
    ) -> None:
        super().__init__(title=title)
        self.game = game
        self.side = side
        self.amount: discord.ui.TextInput = discord.ui.TextInput(
            label=limits_label[:45],
            placeholder="A whole number of coins",
            default=str(default_amount) if default_amount else None,
            max_length=10,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is None:
            return
        amount = parse_amount(str(self.amount.value))
        if amount is None:
            await safe_ephemeral(interaction, "❌ Bets are whole positive numbers.")
            return
        if self.game == "coinflip" and self.side is not None:
            await cog.play_coinflip(interaction, self.side, amount)
        elif self.game == "slots":
            await cog.play_slots(interaction, amount)
        elif self.game == "blackjack":
            await cog.deal_blackjack(interaction, amount)


_ROULETTE_KINDS = {
    "red": ("red", 0),
    "black": ("black", 0),
    "d1": ("dozen", 1),
    "d2": ("dozen", 2),
    "d3": ("dozen", 3),
}


class RouletteBetModal(discord.ui.Modal):
    def __init__(
        self,
        round_id: int,
        kind: str,
        *,
        limits_label: str = "Your bet",
        default_amount: int | None = None,
    ) -> None:
        super().__init__(title="Roulette bet")
        self.round_id = round_id
        self.kind = kind
        self.amount: discord.ui.TextInput = discord.ui.TextInput(
            label=limits_label[:45],
            placeholder="A whole number of coins",
            default=str(default_amount) if default_amount else None,
            max_length=10,
        )
        self.add_item(self.amount)
        self.number: discord.ui.TextInput | None = None
        if kind == "num":
            self.number = discord.ui.TextInput(
                label="Your number (0–36)",
                placeholder="17",
                max_length=2,
            )
            self.add_item(self.number)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is None:
            return
        amount = parse_amount(str(self.amount.value))
        if amount is None:
            await safe_ephemeral(interaction, "❌ Bets are whole positive numbers.")
            return
        if self.kind == "num":
            assert self.number is not None
            raw = str(self.number.value).strip()
            if not raw.isdigit() or not 0 <= int(raw) <= 36:
                await safe_ephemeral(interaction, "❌ Pick a number from 0 to 36.")
                return
            bet_type, selection = "number", int(raw)
        else:
            bet_type, selection = _ROULETTE_KINDS[self.kind]
        await cog.place_roulette_bet(
            interaction, self.round_id, bet_type, selection, amount
        )


# ── the hub panel ──────────────────────────────────────────────────────


class CoinflipSideView(discord.ui.View):
    """Ephemeral heads-or-tails picker; each side opens the amount modal."""

    def __init__(self) -> None:
        super().__init__(timeout=120)

    @discord.ui.button(label="Heads", emoji="🌞", style=discord.ButtonStyle.primary)
    async def heads(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_bet_modal(interaction, "coinflip", side="heads")

    @discord.ui.button(label="Tails", emoji="🌙", style=discord.ButtonStyle.primary)
    async def tails(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_bet_modal(interaction, "coinflip", side="tails")


class CasinoHubView(discord.ui.View):
    """The persistent hub panel — static custom_ids, one registered instance."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Coinflip", emoji="🪙",
        style=discord.ButtonStyle.primary, custom_id="casino:coinflip", row=0,
    )
    async def coinflip(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.send_message(
            "Heads or tails?", view=CoinflipSideView(), ephemeral=True
        )

    @discord.ui.button(
        label="Slots", emoji="🎰",
        style=discord.ButtonStyle.primary, custom_id="casino:slots", row=0,
    )
    async def slots(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_bet_modal(interaction, "slots")

    @discord.ui.button(
        label="Blackjack", emoji="🃏",
        style=discord.ButtonStyle.primary, custom_id="casino:blackjack", row=0,
    )
    async def blackjack(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_bet_modal(interaction, "blackjack")

    @discord.ui.button(
        label="Roulette", emoji="🎡",
        style=discord.ButtonStyle.primary, custom_id="casino:roulette", row=0,
    )
    async def roulette(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_roulette(interaction)

    @discord.ui.button(
        label="My Stats", emoji="📊",
        style=discord.ButtonStyle.secondary, custom_id="casino:stats", row=1,
    )
    async def my_stats(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.send_my_stats(interaction)

    @discord.ui.button(
        label="How It Works", emoji="❓",
        style=discord.ButtonStyle.secondary, custom_id="casino:help", row=1,
    )
    async def help(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.send_help(interaction)


# ── blackjack table buttons ────────────────────────────────────────────

_BJ_STYLES = {
    "hit": ("Hit", discord.ButtonStyle.primary),
    "stand": ("Stand", discord.ButtonStyle.secondary),
    "double": ("Double Down", discord.ButtonStyle.success),
}


class BlackjackActionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"casino_bj:(?P<action>hit|stand|double):(?P<hid>\d+)"),
):
    def __init__(self, action: str, hand_id: int) -> None:
        label, style = _BJ_STYLES[action]
        super().__init__(
            discord.ui.Button(
                label=label, style=style,
                custom_id=f"casino_bj:{action}:{hand_id}",
            )
        )
        self.action = action
        self.hand_id = hand_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> BlackjackActionButton:
        return cls(match["action"], int(match["hid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.blackjack_action(interaction, self.hand_id, self.action)


def build_blackjack_view(hand_id: int, *, can_double: bool) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(BlackjackActionButton("hit", hand_id))
    view.add_item(BlackjackActionButton("stand", hand_id))
    if can_double:
        view.add_item(BlackjackActionButton("double", hand_id))
    return view


# ── roulette round buttons ─────────────────────────────────────────────

_RL_SPECS: dict[str, tuple[str, str | None, discord.ButtonStyle, int]] = {
    "red": ("Red", "🔴", discord.ButtonStyle.danger, 0),
    "black": ("Black", "⚫", discord.ButtonStyle.secondary, 0),
    "num": ("Number", "🎯", discord.ButtonStyle.primary, 0),
    "d1": ("1–12", None, discord.ButtonStyle.secondary, 1),
    "d2": ("13–24", None, discord.ButtonStyle.secondary, 1),
    "d3": ("25–36", None, discord.ButtonStyle.secondary, 1),
}


class RouletteBetButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"casino_rl:(?P<kind>red|black|num|d1|d2|d3):(?P<rid>\d+)"),
):
    def __init__(self, kind: str, round_id: int) -> None:
        label, emoji, style, row = _RL_SPECS[kind]
        super().__init__(
            discord.ui.Button(
                label=label, emoji=emoji, style=style, row=row,
                custom_id=f"casino_rl:{kind}:{round_id}",
            )
        )
        self.kind = kind
        self.round_id = round_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> RouletteBetButton:
        return cls(match["kind"], int(match["rid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_roulette_bet_modal(
                interaction, self.round_id, self.kind
            )


def build_roulette_view(round_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for kind in ("red", "black", "num", "d1", "d2", "d3"):
        view.add_item(RouletteBetButton(kind, round_id))
    return view


# ── the loop-closers: Play Again / Next Round ──────────────────────────

_AGAIN_LABELS = {
    "coinflip": "Flip again",
    "slots": "Spin again",
    "blackjack": "Deal again",
}


class PlayAgainButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(
        r"casino_again:(?P<game>coinflip|slots|blackjack)"
        r":(?P<side>heads|tails|x):(?P<amt>\d+)"
    ),
):
    """On every instant/blackjack result: replay the same bet — for
    WHOEVER clicks (their own coins, every guard re-applies), which turns
    each result into a "me too" invitation rather than a dead end."""

    def __init__(self, game: str, side: str, amount: int) -> None:
        side_note = f" · {side}" if game == "coinflip" else ""
        super().__init__(
            discord.ui.Button(
                label=f"{_AGAIN_LABELS[game]} ({amount:,}{side_note})",
                emoji="🔁",
                style=discord.ButtonStyle.secondary,
                custom_id=f"casino_again:{game}:{side}:{amount}",
            )
        )
        self.game = game
        self.side = side
        self.amount = amount

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> PlayAgainButton:
        return cls(match["game"], match["side"], int(match["amt"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is None:
            return
        if self.game == "coinflip":
            await cog.play_coinflip(interaction, self.side, self.amount)
        elif self.game == "slots":
            await cog.play_slots(interaction, self.amount)
        else:
            await cog.deal_blackjack(interaction, self.amount)


def play_again_view(game: str, amount: int, side: str = "x") -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(PlayAgainButton(game, side, amount))
    return view


class RouletteNextView(discord.ui.View):
    """One persistent button on round recaps — the next round is a click."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Next Round", emoji="🎡",
        style=discord.ButtonStyle.secondary, custom_id="casino:roulette_next",
    )
    async def next_round(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_roulette(interaction)
