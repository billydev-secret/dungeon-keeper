"""Views and modals for the casino — Discord glue only.

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

from bot_modules.services import casino_logic as logic
from bot_modules.services import casino_service as svc
from bot_modules.services import pools_logic

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


class _AmountBetModal(discord.ui.Modal):
    """One amount box + the shared parse/apologize flow.

    The label carries the live limits ("Your bet (5–100 · 340 left today)")
    and the box pre-fills the member's last bet on this game — nobody
    should learn about a limit from the error after submitting. Subclasses
    set the title, stash their routing fields, and implement ``_place``.
    """

    def __init__(
        self,
        *,
        title: str,
        limits_label: str = "Your bet",
        default_amount: int | None = None,
    ) -> None:
        super().__init__(title=title[:45])
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
        await self._place(cog, interaction, amount)

    async def _place(
        self, cog: CasinoCog, interaction: discord.Interaction, amount: int
    ) -> None:
        raise NotImplementedError


class BetModal(_AmountBetModal):
    """``game`` (+ coinflip's ``side``) decides the instant table."""

    def __init__(
        self,
        *,
        title: str,
        game: str,
        side: str | None = None,
        limits_label: str = "Your bet",
        default_amount: int | None = None,
    ) -> None:
        super().__init__(
            title=title, limits_label=limits_label, default_amount=default_amount
        )
        self.game = game
        self.side = side

    async def _place(
        self, cog: CasinoCog, interaction: discord.Interaction, amount: int
    ) -> None:
        if self.game == "coinflip" and self.side is not None:
            await cog.play_coinflip(interaction, self.side, amount)
        elif self.game == "slots":
            await cog.play_slots(interaction, amount)
        elif self.game == "blackjack":
            await cog.deal_blackjack(interaction, amount)
        elif self.game == "war":
            await cog.play_war(interaction, amount)


_ROULETTE_KINDS = {
    "red": ("red", 0),
    "black": ("black", 0),
    "d1": ("dozen", 1),
    "d2": ("dozen", 2),
    "d3": ("dozen", 3),
}


class RouletteBetModal(_AmountBetModal):
    """Amount box, plus the straight-number box when the bet needs one."""

    def __init__(
        self,
        round_id: int,
        kind: str,
        *,
        limits_label: str = "Your bet",
        default_amount: int | None = None,
    ) -> None:
        super().__init__(
            title="Roulette bet",
            limits_label=limits_label, default_amount=default_amount,
        )
        self.round_id = round_id
        self.kind = kind
        self.number: discord.ui.TextInput | None = None
        if kind == "num":
            self.number = discord.ui.TextInput(
                label="Your number (0–36)",
                placeholder="17",
                max_length=2,
            )
            self.add_item(self.number)

    async def _place(
        self, cog: CasinoCog, interaction: discord.Interaction, amount: int
    ) -> None:
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


class DerbyBetModal(_AmountBetModal):
    """One amount box; the runner was chosen by the button that opened it."""

    def __init__(
        self,
        round_id: int,
        runner: int,
        runner_label: str,
        **kwargs,
    ) -> None:
        super().__init__(title=f"Back {runner_label}", **kwargs)
        self.round_id = round_id
        self.runner = runner

    async def _place(
        self, cog: CasinoCog, interaction: discord.Interaction, amount: int
    ) -> None:
        await cog.place_derby_bet(interaction, self.round_id, self.runner, amount)


class BaccaratBetModal(_AmountBetModal):
    """One amount box; the side was chosen by the button that opened it."""

    def __init__(
        self,
        round_id: int,
        side: str,
        side_label: str,
        **kwargs,
    ) -> None:
        super().__init__(title=f"Back {side_label}", **kwargs)
        self.round_id = round_id
        self.side = side

    async def _place(
        self, cog: CasinoCog, interaction: discord.Interaction, amount: int
    ) -> None:
        await cog.place_baccarat_bet(
            interaction, self.round_id, self.side, amount
        )


class DiceBetModal(_AmountBetModal):
    """One amount box; the call was chosen by the button that opened it."""

    def __init__(
        self,
        round_id: int,
        bet_type: str,
        call_label: str,
        **kwargs,
    ) -> None:
        super().__init__(title=f"Call {call_label}", **kwargs)
        self.round_id = round_id
        self.bet_type = bet_type

    async def _place(
        self, cog: CasinoCog, interaction: discord.Interaction, amount: int
    ) -> None:
        await cog.place_dice_bet(
            interaction, self.round_id, self.bet_type, amount
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
        label="Derby", emoji="🏇",
        style=discord.ButtonStyle.primary, custom_id="casino:derby", row=0,
    )
    async def derby(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_derby(interaction)

    @discord.ui.button(
        label="Baccarat", emoji="🎴",
        style=discord.ButtonStyle.primary, custom_id="casino:baccarat", row=1,
    )
    async def baccarat(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_baccarat(interaction)

    @discord.ui.button(
        label="Dice", emoji="🎲",
        style=discord.ButtonStyle.primary, custom_id="casino:dice", row=1,
    )
    async def dice(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_dice(interaction)

    @discord.ui.button(
        label="War", emoji="⚔️",
        style=discord.ButtonStyle.primary, custom_id="casino:war", row=1,
    )
    async def war(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_bet_modal(interaction, "war")

    @discord.ui.button(
        label="Keno", emoji="🔢",
        style=discord.ButtonStyle.primary, custom_id="casino:keno", row=1,
    )
    async def keno(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_keno(interaction)

    @discord.ui.button(
        label="My Stats", emoji="📊",
        style=discord.ButtonStyle.secondary, custom_id="casino:stats", row=2,
    )
    async def my_stats(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.send_my_stats(interaction)

    @discord.ui.button(
        label="How It Works", emoji="❓",
        style=discord.ButtonStyle.secondary, custom_id="casino:help", row=2,
    )
    async def help(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.send_help(interaction)


def build_hub_view(settings: svc.CasinoSettings) -> CasinoHubView:
    """The hub panel's view for ONE guild: disabled tables drop off.

    The full CasinoHubView stays registered at cog_load so buttons on a
    stale panel still route after a restart or re-enable; this pared copy
    is what actually gets sent — making "closed tables disappear from the
    panel" true for the buttons, not just the embed's Tables text.
    """
    view = CasinoHubView()
    for item in list(view.children):
        custom_id = getattr(item, "custom_id", "") or ""
        game = custom_id.removeprefix("casino:")
        if game in svc.GAMES and not svc.game_enabled(settings, game):
            view.remove_item(item)
    return view


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


# ── derby race buttons ─────────────────────────────────────────────────


class DerbyBetButton(
    discord.ui.DynamicItem[discord.ui.Button],
    # The runner range is anchored in the template itself (the roulette
    # buttons' enumerated-alternation rule): a stale button minted for a
    # runner the field no longer has must fail the match, not IndexError
    # inside dispatch.
    template=re.compile(
        rf"casino_dy:(?P<runner>[0-{len(logic.DERBY_FIELD) - 1}]):(?P<rid>\d+)"
    ),
):
    def __init__(self, runner: int, round_id: int) -> None:
        r = logic.DERBY_FIELD[runner]
        super().__init__(
            discord.ui.Button(
                label=f"{r.name.split()[0]} {logic.derby_odds_label(runner)}",
                emoji=r.emoji,
                style=discord.ButtonStyle.secondary,
                row=runner // 3,
                custom_id=f"casino_dy:{runner}:{round_id}",
            )
        )
        self.runner = runner
        self.round_id = round_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> DerbyBetButton:
        return cls(int(match["runner"]), int(match["rid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_derby_bet_modal(
                interaction, self.round_id, self.runner
            )


def build_derby_view(round_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for runner in range(len(logic.DERBY_FIELD)):
        view.add_item(DerbyBetButton(runner, round_id))
    return view


class DerbyNextView(discord.ui.View):
    """One persistent button on race recaps — the next race is a click."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Next Race", emoji="🏇",
        style=discord.ButtonStyle.secondary, custom_id="casino:derby_next",
    )
    async def next_race(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_derby(interaction)


# ── baccarat coup buttons ──────────────────────────────────────────────

_BC_SPECS: dict[str, tuple[str, str, discord.ButtonStyle]] = {
    "player": ("Player 2×", "🔵", discord.ButtonStyle.primary),
    "banker": ("Banker 2×", "🔴", discord.ButtonStyle.danger),
    "tie": ("Tie 9×", "🟡", discord.ButtonStyle.secondary),
}


class BaccaratBetButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"casino_bc:(?P<side>player|banker|tie):(?P<rid>\d+)"),
):
    def __init__(self, side: str, round_id: int) -> None:
        label, emoji, style = _BC_SPECS[side]
        super().__init__(
            discord.ui.Button(
                label=label, emoji=emoji, style=style,
                custom_id=f"casino_bc:{side}:{round_id}",
            )
        )
        self.side = side
        self.round_id = round_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> BaccaratBetButton:
        return cls(match["side"], int(match["rid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_baccarat_bet_modal(
                interaction, self.round_id, self.side
            )


def build_baccarat_view(round_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for side in logic.BACCARAT_SIDES:
        view.add_item(BaccaratBetButton(side, round_id))
    return view


class BaccaratNextView(discord.ui.View):
    """One persistent button on coup recaps — the next hand is a click."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Next Hand", emoji="🎴",
        style=discord.ButtonStyle.secondary, custom_id="casino:baccarat_next",
    )
    async def next_hand(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_baccarat(interaction)


# ── dice roll buttons ──────────────────────────────────────────────────

_DC_SPECS: dict[str, tuple[str, str | None, discord.ButtonStyle, int]] = {
    "big": ("Big 11–17", "⬆️", discord.ButtonStyle.primary, 0),
    "small": ("Small 4–10", "⬇️", discord.ButtonStyle.primary, 0),
    "odd": ("Odd", None, discord.ButtonStyle.secondary, 1),
    "even": ("Even", None, discord.ButtonStyle.secondary, 1),
}


class DiceBetButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"casino_dc:(?P<kind>big|small|odd|even):(?P<rid>\d+)"),
):
    def __init__(self, kind: str, round_id: int) -> None:
        label, emoji, style, row = _DC_SPECS[kind]
        super().__init__(
            discord.ui.Button(
                label=label, emoji=emoji, style=style, row=row,
                custom_id=f"casino_dc:{kind}:{round_id}",
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
    ) -> DiceBetButton:
        return cls(match["kind"], int(match["rid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_dice_bet_modal(
                interaction, self.round_id, self.kind
            )


def build_dice_view(round_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for kind in logic.SICBO_BET_TYPES:
        view.add_item(DiceBetButton(kind, round_id))
    return view


class DiceNextView(discord.ui.View):
    """One persistent button on roll recaps — the next roll is a click."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Next Roll", emoji="🎲",
        style=discord.ButtonStyle.secondary, custom_id="casino:dice_next",
    )
    async def next_roll(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_dice(interaction)


# ── keno ticket buttons ────────────────────────────────────────────────


class KenoTicketModal(_AmountBetModal):
    """One amount box; the tier was chosen by the button that opened it."""

    def __init__(self, round_id: int, spots: int, **kwargs) -> None:
        super().__init__(title=f"Keno — Pick {spots}", **kwargs)
        self.round_id = round_id
        self.spots = spots

    async def _place(
        self, cog: CasinoCog, interaction: discord.Interaction, amount: int
    ) -> None:
        await cog.place_keno_ticket(
            interaction, self.round_id, self.spots, amount
        )


class KenoTierButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"casino_kn:(?P<spots>4|6|8|10):(?P<rid>\d+)"),
):
    def __init__(self, spots: int, round_id: int) -> None:
        top = max(logic.KENO_PAYTABLE[spots].values())
        super().__init__(
            discord.ui.Button(
                label=f"Pick {spots} · to {top}×",
                emoji="🎟️",
                style=discord.ButtonStyle.primary,
                custom_id=f"casino_kn:{spots}:{round_id}",
            )
        )
        self.spots = spots
        self.round_id = round_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> KenoTierButton:
        return cls(int(match["spots"]), int(match["rid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_keno_ticket_modal(
                interaction, self.round_id, self.spots
            )


def build_keno_view(round_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for spots in logic.KENO_TIERS:
        view.add_item(KenoTierButton(spots, round_id))
    return view


class KenoNextView(discord.ui.View):
    """One persistent button on draw recaps — the next draw is a click."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Next Draw", emoji="🔢",
        style=discord.ButtonStyle.secondary, custom_id="casino:keno_next",
    )
    async def next_draw(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_keno(interaction)


# ── war standoff buttons ───────────────────────────────────────────────

_WAR_STYLES = {
    "war": ("Go to War", "⚔️", discord.ButtonStyle.danger),
    "retreat": ("Retreat", "🏳️", discord.ButtonStyle.secondary),
}


class WarActionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"casino_wr:(?P<action>war|retreat):(?P<hid>\d+)"),
):
    def __init__(self, action: str, hand_id: int) -> None:
        label, emoji, style = _WAR_STYLES[action]
        super().__init__(
            discord.ui.Button(
                label=label, emoji=emoji, style=style,
                custom_id=f"casino_wr:{action}:{hand_id}",
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
    ) -> WarActionButton:
        return cls(match["action"], int(match["hid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.war_action(interaction, self.hand_id, self.action)


def build_war_view(hand_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(WarActionButton("war", hand_id))
    view.add_item(WarActionButton("retreat", hand_id))
    return view


# ── the loop-closers: Play Again / Next Round ──────────────────────────

_AGAIN_LABELS = {
    "coinflip": "Flip again",
    "slots": "Spin again",
    "blackjack": "Deal again",
    "war": "Battle again",
}


class PlayAgainButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(
        r"casino_again:(?P<game>coinflip|slots|blackjack|war)"
        r":(?P<side>heads|tails|x):(?P<amt>\d+)"
    ),
):
    """On every instant/blackjack result: replay the same bet — for
    WHOEVER clicks (their own coins, every guard re-applies). On your own
    ephemeral machine it spins the same message in place; on a public
    big-win broadcast it opens the clicker's own machine — the "me too"
    invitation surviving the ephemeral move."""

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
        elif self.game == "war":
            await cog.play_war(interaction, self.amount)
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


# ── pools (casino-classics Stage 2) ────────────────────────────────────


class PoolsBetModal(_AmountBetModal):
    """One amount box; the side was chosen by the button that opened it."""

    def __init__(self, side: str, **kwargs) -> None:
        super().__init__(
            title=f"Bet {pools_logic.describe_side(side)}", **kwargs
        )
        self.side = side

    async def _place(
        self, cog: CasinoCog, interaction: discord.Interaction, amount: int
    ) -> None:
        await cog.place_pools_bet(interaction, self.side, amount)


class PoolsPanelView(discord.ui.View):
    """The two standing buttons on the daily market panel.

    No round id in the custom ids: there is exactly one open market per
    guild at a time, so the handler resolves it at click. That also means
    the panel keeps working across a restart and across the day roll
    without re-registering anything.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Bet Over", emoji="📈",
        style=discord.ButtonStyle.success, custom_id="casino:pools_over",
    )
    async def bet_over(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_pools_bet(interaction, pools_logic.OVER)

    @discord.ui.button(
        label="Bet Under", emoji="📉",
        style=discord.ButtonStyle.danger, custom_id="casino:pools_under",
    )
    async def bet_under(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        cog = await _dispatch_or_apologize(interaction)
        if cog is not None:
            await cog.open_pools_bet(interaction, pools_logic.UNDER)
