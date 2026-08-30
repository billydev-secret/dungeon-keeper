"""Economy service — DB layer for wallets, the ledger, and per-guild settings.

Soft-currency balances, a signed audit ledger, and balance-change DM mute
prefs, plus the per-guild ``econ_`` settings stored in the shared config KV
table. See docs/economy_spec.md for the feature design.
"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import time
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING

from bot_modules.economy import live_signal, logic
from bot_modules.economy.kinds import UNSCALED_CREDIT_KINDS

if TYPE_CHECKING:
    from pathlib import Path

    import discord

ECON_PREFIX = "econ_"

#: The shop lines an admin can switch off one at a time, in shop display order.
#: Each one owns a ``shop_<perk>_enabled`` field on :class:`EconSettings`.
#:
#: Seven of the eight are rentable perks; ``streak_shield`` is the one-shot
#: consumable, which shares the vocabulary because it shares the checkbox. The
#: raffle and the guild's custom shop items are deliberately absent — they came
#: with their own switches (``raffle_enabled`` and a per-item ``enabled``
#: column), and a second control for the same thing is the overload this
#: feature exists to remove.
#:
#: It lives here rather than in ``economy/perks.py`` because that module
#: imports this one for :class:`EconSettings`; the perk vocabulary re-exports
#: it so callers can keep reading perk facts from one place.
SHOP_TOGGLE_PERKS: tuple[str, ...] = (
    "role_color",
    "role_name",
    "role_preset",
    "role_gradient",
    "role_holographic",
    "role_icon",
    "voice_style",
    "streak_shield",
)


@dataclass(frozen=True)
class EconSettings:
    enabled: bool = False
    bank_channel_id: int = 0
    manager_role_id: int = 0
    # Opt-in economy-notifications role, toggled by the guide panel's 🔔
    # button. It is a **DM preference only** — it gates no channel, no payout
    # and no command. When set, auto-claimed quest completions (trigger-word /
    # photo-reply / media-post) DM the claimant their card instead of replying
    # in-channel, and recurring engagement notices (streaks, milestones) reach
    # holders only; everyone else still gets the in-channel reaction + reply.
    # 0 (default) = nobody has opted in, so the recurring DMs go to nobody.
    game_role_id: int = 0
    # Keep the morning login digest DM current instead of leaving it a
    # snapshot: an hourly pass re-renders the card in place as quest progress
    # lands, and stops once the member has cleared their personal quests.
    # Edits are silent — Discord raises no notification for them — so this
    # never adds a ping, and it never posts a second message. Off makes the
    # card a one-shot snapshot again (it is still sent); it does not disable
    # the digest itself, which is what game_role_id above governs.
    login_card_live_updates: bool = True
    # The QOTD role — one dial doing two jobs. ``/qotd post`` pings it, AND
    # any message from a mod that tags it registers as that day's question,
    # so a mod can just ask in their own words instead of running a command.
    # Replies to a registered question are what pay ``reward_qotd``. 0
    # (default) = no ping and no auto-registration. The role must be
    # mentionable (or the bot must hold "Mention @everyone, @here, and All
    # Roles"), else Discord renders the mention as inert text.
    qotd_ping_role_id: int = 0
    currency_name: str = "Coin"
    currency_plural: str = "Coins"
    currency_emoji: str = "🪙"
    currency_icon_url: str = ""
    wallet_name: str = "Wallet"
    transfers_enabled: bool = True
    booster_multiplier: float = 1.5
    # One dial over every earned faucet, as a percentage (100 = ship rate).
    # Retuning the economy used to mean editing ~14 separate dials, which is
    # why the 2026-07-30 retune quietly went stale when the member base grew:
    # per-earner minting fell 41% while headcount rose 59%, so the float kept
    # climbing (docs/reviews/2026-08-28-economy-affordability-review.md §1).
    # This scales all of them at once, so a retune is one number and one
    # checkpoint. Applies to earned income only — see UNSCALED_CREDIT_KINDS
    # for what it must never touch. It is a *rate*, not an off-switch: a
    # faucet that would round to nothing still pays 1, and each faucet keeps
    # its own zero/enable dial for turning it off outright.
    faucet_scale_pct: int = 100
    # XP → coin conversion rate (XP per coin). Ships at 0 = the faucet is OFF:
    # earning XP no longer mints currency. An admin re-enables it by setting a
    # positive rate on the Income Sources panel; the day-roll driver skips the
    # conversion entirely while the rate is 0, so nothing accumulates and a
    # later re-enable resumes cleanly from that day rather than dumping a
    # backlog. The mechanism (convert_xp/process_conversion) is retained intact.
    xp_per_coin: float = 0.0
    # Ceiling on what one member's XP can mint in a single day (0 = none).
    # Conversion is the only faucet with no natural bound: a login fires once,
    # drops have drops_per_day, a quest board is finite, but this one scales
    # linearly with however much XP someone earns — on 2026-07-25 it paid one
    # member 932 in a day and 60% of the guild's entire mint. Without a
    # ceiling the rate is the only brake, and the rate hits the quiet member
    # as hard as the top chatter.
    #
    # Coins past the ceiling are DISCARDED, remainder included — a cap that
    # banks the overflow is a delay, not a limit, and the backlog lands in one
    # lump the day it lifts. Like every other faucet rate this is a
    # pre-booster base, so a booster's 1.5x applies on top of the ceiling.
    conversion_daily_cap: int = 0
    login_text_base: int = 5
    login_voice_base: int = 15
    streak_bonus_cap: int = 10
    milestone_day7: int = 25
    milestone_day30: int = 100
    milestone_day100: int = 365
    milestone_per_100: int = 100
    reward_qotd: int = 10
    reward_game_participation: int = 5
    reward_game_win: int = 20
    # External CAH (Gamebot) score payout: replaces the flat participation/win
    # amounts above for CAH games only. The top scorer (the *Game over!*
    # winner) earns this many coins; everyone else earns it scaled by their
    # score's ratio to the winner's, rounded to the nearest coin (a share that
    # rounds to 0 pays nothing). 0 turns the payout off for CAH entirely.
    reward_cah_win_max: int = 50
    # Cat Bot catch payouts by rarity tier — games_external/parser groups the
    # 22 cat types into these six tiers. Hardcoded in the parser until
    # 2026-08-06, when the 07-30 retune postmortem found cat_catch had become
    # the #2 faucet with no dial (loose-ends §1). Defaults are the shipped
    # table; tune from the Income Sources panel.
    catcatch_coins_common: int = 1
    catcatch_coins_uncommon: int = 3
    catcatch_coins_rare: int = 11
    catcatch_coins_epic: int = 35
    catcatch_coins_mythic: int = 102
    catcatch_coins_divine: int = 300
    # Per-member, per-guild-local-day ceiling on cat coins; 0 = uncapped.
    # The per-tier dials above scale everyone equally, which is the wrong
    # shape for a volume faucet — this is the one that bites the farmer and
    # leaves the casual catcher alone. Staged into prod config 2026-08-02;
    # the enforcing code only reached main 2026-08-06 (see
    # docs/reviews/2026-08-06-economy-ledger-data-audit.md H2).
    cat_catch_daily_cap: int = 0
    # Host bounty: the member who *ran* a game earns per attendee who joined
    # (excluding themselves), capped at ``host_bounty_cap`` attendees so one
    # busy game can't dwarf other faucets. The point is recruiting hosts, so
    # it only fires for a game that someone actually joined — a host talking to
    # themselves earns nothing, which also closes the empty-game farm. 0 rate
    # (default) ships it dark; gated by the game_host income-source toggle.
    host_bounty_per_joiner: int = 0
    host_bounty_cap: int = 5
    # Flat participation award for posting an image in the Photo Challenge
    # channel — paid on the post itself, once per guild-local day, on top of
    # any active photo_post quest (which stacks). 0 turns the flat award off
    # (the quest, if any, still pays). Gated by the photo_post income-source
    # toggle like the quest is.
    reward_photo_post: int = 5
    # Flat award to the greeter for each intake-card checklist step they tick
    # (see bot_modules/economy/intake_rewards.py). Paid per step rather than
    # per finished card so two greeters sharing one welcome each get paid for
    # what they actually did. Only steps with a real actor pay: the auto-tick
    # hooks for `verified`/`role_gained` record AUTO_ACTOR (0) and credit
    # nobody. Anchored once per (card, step) in econ_intake_rewards, so
    # toggling a step off and on again mints nothing. 0 turns it off; gated by
    # the intake_step income-source toggle.
    reward_intake_step: int = 5
    # Coin Drops (see economy_drops_service/_loop): the bot drops a pouch of
    # coins in this channel at random moments; the first member to reply to
    # the drop message claims it. The channel picker is the toggle — 0
    # (default) = no drops. ``drops_per_day`` is an *average* cadence (each
    # gap is jittered 0.5–1.5×, and a drop also waits for someone to have
    # spoken since the bot's own last message, so dead hours drop nothing);
    # amounts roll uniformly in [min, max]; unclaimed pouches expire after
    # ``drops_expire_minutes`` and pay nobody.
    drops_channel_id: int = 0
    drops_min_coins: int = 5
    drops_max_coins: int = 25
    drops_per_day: int = 4
    drops_expire_minutes: int = 60
    # How many quests of each cadence a member is shown (and can be paid for)
    # per period — their "personal board", drawn from that cadence's active
    # pool. Tuning these down is how a guild makes the board feel smaller
    # without deactivating library quests; 0 turns the cadence off entirely
    # (nothing shows, nothing pays). Capped at POOL_CAP by the dashboard.
    # (No monthly size: a monthly quest is a guild-wide goal, never a personal
    # board row, so quest_board_monthly sized nothing. It was dropped rather
    # than left as a dial the panel no longer offers and no draw ever reads;
    # any stored econ_quest_board_monthly row is ignored by the loader.)
    quest_board_daily: int = 2
    quest_board_weekly: int = 2
    # NOTE: quest_board_monthly is gone. Monthly became a single guild-wide
    # goal (docs/plans/monthly-community-quests.md), so there is no monthly
    # personal board to size: the draw stopped reading it and the leaderboard's
    # summary row can never render one. Stored `econ_quest_board_monthly` rows
    # are ignored by the loader.    # Community-weekly beat sheets (kickoff / tier crossed / final-24h /
    # resolution) DM this member so they can host the event in their own
    # voice — the bot posts nothing publicly (2026-07-18 decision). 0 =
    # fall back to the guild owner.
    community_host_user_id: int = 0
    # Clear-the-board set bonuses: paid once per period when a member
    # completes EVERY quest on their personal board of that cadence
    # (ledger kind quest_bonus, no booster multiplier). Default OFF — a
    # silent default-on bonus surprises small boards (a 1-quest pool pays
    # it on every claim); guilds opt in on the Settings page (the main
    # guild is seeded 10/25 by scripts/seed_quest_variety.py).
    quest_set_bonus_daily: int = 0
    quest_set_bonus_weekly: int = 0
    # Paid board rerolls, bought after the one free reroll each guild-local
    # day. The cap is the point: unlimited paid rerolls let a wealthy member
    # cycle the board hunting for the cheapest quests, which turns a "this
    # one doesn't fit how I use the server" escape hatch into a shopping
    # trip. Either value at 0 disables paid rerolls (the free one stays).
    price_quest_reroll: int = 10
    quest_reroll_daily_cap: int = 3
    # Sponsor-a-QOTD: a member pays to put a question in front of the server,
    # a mod approves it first. Charged at submit (a free queue invites spam),
    # so denial and expiry refund. 0 disables sponsoring entirely. Pending
    # submissions nobody resolves expire and refund after this many days;
    # approved ones never expire — they're waiting on staff, not the member.
    price_qotd_sponsor: int = 40
    qotd_sponsor_expire_days: int = 14
    # Pin of the Day (plan: docs/plans/pin-of-the-day.md): a member pays to pin a
    # short message; a mod approves it; the bot pins a card in `pin_channel_id`
    # for 24h, then auto-unpins. A public sink — off until BOTH a price and a
    # channel are set (announce before flipping it on). Charged at submit, so
    # denial and pending-expiry refund; a pin that went live does not. Pending
    # submissions nobody resolves expire and refund after `pin_expire_days`.
    price_pin_of_day: int = 0
    pin_channel_id: int = 0
    pin_expire_days: int = 3
    # Flash Themes (migration 188): a member pays to name the day's theme; a
    # mod approves it; approved themes queue and the hourly loop runs the
    # oldest one whenever `theme_channel_id` is free, posting ONE card there
    # and pinning it for `theme_hours`. An empty queue posts nothing — a day
    # with no theme is simply a normal day.
    #
    # Unlike the two above, price 0 does NOT switch this off:
    # `flash_theme_enabled` is a real toggle, following the per-perk switches
    # added in migration 182 precisely so a zero price stops meaning two
    # things at once. A free themed day is a legitimate thing to run.
    #
    # Charged at submit, so denial and pending-expiry refund; a theme that ran
    # its window does not. 300 is provisional and sits with the premium tier
    # (holographic roles, room rentals) rather than the small consumables —
    # set it per guild on the dashboard.
    flash_theme_enabled: bool = False
    price_flash_theme: int = 300
    theme_channel_id: int = 0
    theme_expire_days: int = 3
    theme_hours: int = 24
    # Community Bounty (plan: docs/plans/community-bounty.md): anyone posts a
    # freeform task and seeds a pot; anyone chips in; a mod awards the pot to the
    # winner minus `bounty_rake_pct` (which evaporates — a real sink, next to the
    # wager rake / hoard tax); an unawarded bounty refunds every contributor
    # after `bounty_expire_days`. Off until a board channel is set.
    bounty_channel_id: int = 0
    bounty_min_stake: int = 10
    bounty_max_open: int = 3
    bounty_expire_days: int = 14
    bounty_rake_pct: int = 0
    #: Where the hub panel — the board's only entry point since `/bounty` was
    #: deleted — was actually posted. The channel is stored *alongside* the
    #: message rather than inferred from `bounty_channel_id`, because an admin
    #: can repoint the board: on a mismatch EconomyCog._bounty_panel_ids reads
    #: the old hub as unposted, so the restick stops chasing a message that is
    #: no longer on the board (which would otherwise leave two live hubs).
    bounty_panel_channel_id: int = 0
    bounty_panel_message_id: int = 0
    # Live auctions (plan: docs/plans/economy-auctions.md): a mod opens a
    # freeform, mod-fulfilled auction with `/bank auction start`; members bid up
    # in the open, the outbid bidder is refunded instantly, and the winning bid
    # is burned (the sink). `min_bid` is the opening floor; each new bid must
    # beat the standing high by `min_increment`. `soft_close_seconds` is the
    # anti-snipe window — a bid landing that close to the end pushes the end out
    # by the same amount. `max_duration_hours` guard-rails what a mod can set.
    # Naturally dark: no auction exists until a mod opens one, so no kill switch.
    auction_min_bid: int = 10
    auction_min_increment: int = 5
    auction_soft_close_seconds: int = 300
    auction_max_duration_hours: int = 168
    # ── What the shop actually sells ──────────────────────────────────────
    # One checkbox per rentable perk (plus the one-shot shield), set on the
    # Shop & Perks page. Unchecked = not for sale: the row leaves the shop
    # embed and the picker, `rent_perk`/`purchase_streak_shield` refuse, the staff
    # comp stops entitling it, and a live rental runs to its anniversary and
    # then stops renewing instead of being cut off mid-week (economy spec §6).
    #
    # These are the ONLY off switch. Price 0 used to double as one for the
    # shield (hid the row) and the voice lease (disarmed the paywall), which
    # meant the same 0 meant "hidden" on one dial and "free for everyone" on
    # another; migration 182 moved every guild onto the checkbox and 0 went
    # back to meaning nothing but a price of zero.
    #
    # voice_style defaults OFF because its price defaults to 0 — a guild that
    # has never touched the economy keeps its free rename/user-limit controls
    # rather than waking up with a paywall. Everything else defaults ON, which
    # is what those guilds already had.
    shop_role_color_enabled: bool = True
    shop_role_name_enabled: bool = True
    shop_role_preset_enabled: bool = True
    shop_role_gradient_enabled: bool = True
    shop_role_holographic_enabled: bool = True
    shop_role_icon_enabled: bool = True
    shop_voice_style_enabled: bool = False
    shop_streak_shield_enabled: bool = True

    # Prepaid streak shield (sinks round 3, stage 2): a one-shot consumable
    # held (max 1) until a login gap would reset the streak, then auto-burned
    # to save it — covers what the free grace day can't. 0 hides the shop row
    # (a shield already held still works).
    price_streak_shield: int = 30
    price_role_color: int = 50
    price_role_name: int = 35
    price_role_icon: int = 75
    # A colour from the curated palette (`econ_color_catalog`, the old booster
    # cosmetic roles). Priced well under the free-form gradient it undercuts: the
    # palette is the value pick, picking your own two colours is the splurge. A
    # palette colour may also carry its own price, which wins over this flat one.
    price_role_preset: int = 80
    # Raised from 120 with the palette's arrival (todo #76) so the curated set
    # reads as the cheaper option rather than a near-identical one. Existing
    # renters move to the new price at their next renewal, and the billing loop
    # DMs them the old and new figure (economy A2).
    price_role_gradient: int = 240
    # Discord's holographic role preset — a fixed three-colour shimmer set via
    # `tertiary_colour`, not the member-picked two-colour gradient. Priced above
    # the gradient as the top cosmetic tier; like the gradient it needs the
    # guild's ENHANCED_ROLE_COLORS feature to actually render.
    price_role_holographic: int = 300
    # Voice-style lease (sinks round 3, stage 3): Voice Control rename + user
    # limit become leased while this is > 0 AND the economy is enabled. The
    # 0 default is the dark launch — controls stay free until an admin prices
    # the lease on the Sinks page (suggested ≈ 30).
    price_voice_style: int = 0
    # Staff perk comp: while on, anyone `is_mod` counts as (configured mod or
    # admin role, or Discord manage_guild/administrator) is entitled to every
    # rentable perk without renting one. No rental row and no ledger row is
    # written — the comp is derived from live role state, so it appears and
    # disappears with the role and never shows up as spend that didn't happen.
    # Off by default: a second guild running its own economy shouldn't start
    # comping its staff because this shipped.
    mod_perk_comp: bool = False
    # Emoji sponsorship (sinks round 3, stage 4): weekly rent to keep a custom
    # emoji in the server, escrowed at submit, mod-approved, deleted on lapse.
    # price_emoji 0 disables the whole feature (running rentals still bill and
    # can lapse). Animated emojis bill their own (richer) rate. The slot cap
    # bounds pending+approved+live sponsorships; expiry refunds a pending
    # submission nobody resolved (mirrors the sponsored-QOTD sweep).
    price_emoji: int = 60
    price_emoji_animated: int = 90
    emoji_sponsor_slots: int = 5
    emoji_sponsor_expire_days: int = 14
    # NOTE: price_text_room / price_voice_room used to sit here, priced for a
    # private-rooms stage that was never built. Nothing could ever be bought
    # with them, so they were dropped rather than left advertising rentals
    # nobody can rent. Stored `econ_price_*_room` rows are simply ignored by
    # the loader — no migration, no product decision reversed.    # Custom shop items (docs/plans/economy-shop-items.md): admin-defined
    # items sold beside the built-in perks. A manual item escrows the price
    # and files a todo; this is how long that order waits for staff before the
    # member gets their coins back (the emoji/QOTD sponsor sweep pattern).
    # 0 disables the sweep rather than expiring every open order at once.
    shop_item_expire_days: int = 14
    # Weekly raffle (sinks round 3, stage 5): week-scoped tickets, weighted
    # draw at the ISO-week roll, prize = a free-perk-week voucher (never
    # coins — ticket revenue is a pure burn). Default OFF; the winner is
    # announced BY NAME on the leaderboard panel (buying in = opting in, the
    # deliberate carve-out from the anonymous-ticker rule), so enable only
    # after telling the server. The cap keeps a whale from buying certainty.
    raffle_enabled: bool = False
    price_raffle_ticket: int = 10
    raffle_max_tickets: int = 10
    # Weekly hoard tax (demurrage): at the ISO-week roll, wallets above the
    # threshold lose rate% of the EXCESS (never the threshold itself — it's a
    # protected floor, so 100 is a hard wealth cap, not confiscation). The
    # only sink that works on members who buy nothing. Rate 0 = off (the
    # dark-launch default); pricing it on the Sinks page is the launch
    # switch — announce first.
    demurrage_rate_pct: int = 0
    demurrage_threshold: int = 500
    # House cut on PvP wager pots (revises the sinks-round-2 no-rake stance):
    # the winner takes pot minus rake%, and the rake evaporates. 0 (the
    # dark-launch default) keeps wagers the original pure transfer; raking
    # makes them a real sink at the cost of the clean "winner takes the pot"
    # promise, so announce before setting it. Never raked: refunds, or a pot
    # holding a single stake (the winner's own ante back is not a contest).
    wager_rake_pct: int = 0
    # Bot-managed bookkeeping for the guild's one economy panel; readable via
    # GET /economy/config but deliberately absent from the dashboard's
    # editable-field whitelist. Named for the how-to guide it used to carry:
    # the guide and leaderboard panels merged on 2026-08-18 and this pair, the
    # surviving message's own ids, is what the merged panel kept. The panel
    # renders the live board (economy/leaderboard.py) and the guide is an ❓
    # button on it.
    guide_channel_id: int = 0
    guide_message_id: int = 0
    # Same pattern for the persistent perk-shop panel (/bank post-shop;
    # buttons are DynamicItems so they survive restarts).
    shop_channel_id: int = 0
    shop_message_id: int = 0
    # Public transaction feed (see economy/register.py). Unset (0) = off; the
    # channel picker IS the toggle. Every econ_ledger row for the guild is
    # posted here as it lands, saying what it was for.
    register_channel_id: int = 0
    # Bot-managed drain cursor: the highest econ_ledger.id already posted to
    # the register. Bookkeeping like the *_message_id fields, so it is
    # deliberately absent from the dashboard's editable whitelist. Seeded to
    # the ledger's current MAX(id) on first drain so enabling the feed never
    # backfills the guild's entire history.
    #
    # -1 (not 0) is the "never seeded" sentinel: 0 is a legitimate seeded
    # cursor for a guild whose ledger is still empty, and conflating the two
    # would re-seed past that guild's first-ever transaction and swallow it.
    register_cursor_id: int = -1


DEFAULT_ECON_SETTINGS = EconSettings()

_BOOL_KEYS = [
    "enabled",
    "transfers_enabled",
    "login_card_live_updates",
    "raffle_enabled",
    "mod_perk_comp",
    # Not a SHOP_TOGGLE_PERKS entry: that tuple drives the shop's own list of
    # rentable perk lines, and a flash theme is a paid submission queue.
    "flash_theme_enabled",
    *(f"shop_{p}_enabled" for p in SHOP_TOGGLE_PERKS),
]
_FLOAT_KEYS = ["booster_multiplier", "xp_per_coin"]
_STR_KEYS = [
    "currency_name",
    "currency_plural",
    "currency_emoji",
    "currency_icon_url",
    "wallet_name",
]
# Everything else on the dataclass is a plain int.
_INT_KEYS = [
    f.name
    for f in fields(EconSettings)
    if f.name not in _BOOL_KEYS and f.name not in _FLOAT_KEYS and f.name not in _STR_KEYS
]

_ALL_KEYS = frozenset(f.name for f in fields(EconSettings))


def load_econ_settings(conn: sqlite3.Connection, guild_id: int) -> EconSettings:
    """Build an EconSettings from stored ``econ_`` config values.

    Guild-scoped only — no legacy guild_id=0 fallback, so an unconfigured
    guild gets real defaults instead of inheriting the legacy guild_id=0
    rows. One query for every ``econ_*`` key (GLOB keeps the underscore
    literal): this loader runs on hot paths — casino bets load it inside
    every stake — and the per-field version was ~35 SELECTs per call.
    """
    from bot_modules.core.db_utils import parse_bool

    stored = {
        str(r["key"])[len(ECON_PREFIX):]: str(r["value"])
        for r in conn.execute(
            "SELECT key, value FROM config WHERE guild_id = ? "
            "AND key GLOB 'econ_*'",
            (guild_id,),
        )
    }
    defaults = DEFAULT_ECON_SETTINGS
    kwargs: dict[str, object] = {}

    for key in _BOOL_KEYS:
        raw = stored.get(key, "")
        if raw:
            kwargs[key] = parse_bool(raw, getattr(defaults, key))

    for key in _INT_KEYS:
        raw = stored.get(key, "")
        if raw:
            try:
                kwargs[key] = int(raw)
            except ValueError:
                pass

    for key in _FLOAT_KEYS:
        raw = stored.get(key, "")
        if raw:
            try:
                kwargs[key] = float(raw)
            except ValueError:
                pass

    for key in _STR_KEYS:
        raw = stored.get(key, "")
        if raw:
            kwargs[key] = raw

    if not kwargs:
        return defaults
    for f in defaults.__dataclass_fields__:
        if f not in kwargs:
            kwargs[f] = getattr(defaults, f)
    return EconSettings(**kwargs)  # type: ignore[arg-type]


def save_econ_settings(
    conn: sqlite3.Connection, guild_id: int, values: dict[str, object]
) -> None:
    """Persist a partial dict of settings under the ``econ_`` prefix.

    Every key must name an EconSettings field; an unknown key raises KeyError
    so callers can't silently write dead config. Booleans persist as "1"/"0".
    """
    from bot_modules.core.db_utils import set_config_value

    unknown = set(values) - _ALL_KEYS
    if unknown:
        raise KeyError(f"unknown econ setting(s): {sorted(unknown)}")

    for key, value in values.items():
        if isinstance(value, bool):
            stored = "1" if value else "0"
        else:
            stored = str(value)
        set_config_value(conn, f"{ECON_PREFIX}{key}", stored, guild_id)


def get_balance(conn: sqlite3.Connection, guild_id: int, user_id: int) -> int:
    row = conn.execute(
        "SELECT balance FROM econ_wallets WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    return int(row["balance"]) if row else 0


def apply_credit(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    amount: int,
    kind: str,
    *,
    actor_id: int | None = None,
    meta: dict | None = None,
    booster: bool = False,
    multiplier: float = 1.5,
    scale_pct: int | None = None,
) -> int:
    """Credit a wallet and record the ledger row as one atomic unit.

    Returns the credited amount: ``ceil(amount * multiplier)`` when ``booster``
    is set, else ``amount``, then scaled by the guild's ``faucet_scale_pct``
    unless ``kind`` is in ``UNSCALED_CREDIT_KINDS`` (money moving sideways, an
    admin grant, or a refund — none of which are the guild's to shave). Raises
    ValueError for ``amount < 1``. Rides the passed connection — the caller's
    transaction is the commit boundary.

    ``scale_pct`` lets a loop crediting many members pass one preloaded read
    instead of reloading settings per credit, the same way ``feed_jackpot``
    takes ``settings``.
    """
    if amount < 1:
        raise ValueError("credit amount must be >= 1")
    credited = math.ceil(amount * multiplier) if booster else amount
    if kind not in UNSCALED_CREDIT_KINDS:
        # Loaded lazily and only for scalable kinds, so the highest-volume
        # credit there is — a casino payout — costs nothing extra. The
        # scale rides *after* the booster: the booster is a member's perk,
        # this is the guild's economy-wide rate, and the rate applies to
        # what the perk produced.
        pct = (
            load_econ_settings(conn, guild_id).faucet_scale_pct
            if scale_pct is None
            else scale_pct
        )
        if pct != 100:
            credited = max(1, credited * max(0, pct) // 100)
    now = time.time()
    conn.execute(
        """
        INSERT INTO econ_wallets (guild_id, user_id, balance, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            balance    = balance + excluded.balance,
            updated_at = excluded.updated_at
        """,
        (guild_id, user_id, credited, now, now),
    )
    conn.execute(
        """
        INSERT INTO econ_ledger
            (guild_id, user_id, amount, kind, actor_id, meta, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id,
            user_id,
            credited,
            kind,
            actor_id,
            json.dumps(meta) if meta is not None else None,
            now,
        ),
    )
    live_signal.mark_dirty(guild_id)
    return credited


def apply_debit(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    amount: int,
    kind: str,
    *,
    actor_id: int | None = None,
    meta: dict | None = None,
) -> bool:
    """Debit a wallet and record the ledger row as one atomic unit.

    Returns False with no writes when the balance is below ``amount`` (or the
    wallet doesn't exist); balances never go negative. Raises ValueError for
    ``amount < 1``.
    """
    if amount < 1:
        raise ValueError("debit amount must be >= 1")
    now = time.time()
    cur = conn.execute(
        """
        UPDATE econ_wallets
        SET balance = balance - ?, updated_at = ?
        WHERE guild_id = ? AND user_id = ? AND balance >= ?
        """,
        (amount, now, guild_id, user_id, amount),
    )
    if (cur.rowcount or 0) == 0:
        return False
    conn.execute(
        """
        INSERT INTO econ_ledger
            (guild_id, user_id, amount, kind, actor_id, meta, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id,
            user_id,
            -amount,
            kind,
            actor_id,
            json.dumps(meta) if meta is not None else None,
            now,
        ),
    )
    return True


def remove_currency(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    amount: int,
    kind: str = "admin_remove",
    *,
    actor_id: int | None = None,
    meta: dict | None = None,
) -> int:
    """Take up to ``amount`` off a wallet, clamped at a zero balance.

    ``apply_debit`` is all-or-nothing because a purchase either happens or it
    doesn't. This is the moderator's correction lever instead: an over-typed
    amount leaves the member at zero rather than failing or owing a debt.

    Returns the amount actually removed — 0 when the wallet is empty or absent,
    and then nothing is written at all (no zero-value ledger row). The ledger
    row records the removed amount, not the requested one, and the booster
    multiplier is deliberately never applied: a removal is a correction, not an
    earning, so a booster isn't penalised 1.5x for the same offence. Raises
    ValueError for ``amount < 1``. The read and the debit are not atomic on
    their own — callers on a money path should ride ``open_db_immediate``; a
    wallet drained in between yields 0, not an overstated figure.
    """
    if amount < 1:
        raise ValueError("remove amount must be >= 1")
    removed = min(amount, get_balance(conn, guild_id, user_id))
    if removed < 1:
        return 0
    # The read above and this guarded UPDATE aren't atomic on their own: a
    # caller not holding the write lock can have the wallet drained in between,
    # and apply_debit then writes nothing. Report 0, never a phantom removal.
    if not apply_debit(
        conn, guild_id, user_id, removed, kind, actor_id=actor_id, meta=meta
    ):
        return 0
    live_signal.mark_dirty(guild_id)
    return removed


def transfer_currency(
    conn: sqlite3.Connection,
    guild_id: int,
    from_id: int,
    to_id: int,
    amount: int,
    *,
    memo: str | None = None,
) -> None:
    """Move ``amount`` between two wallets as one atomic debit + credit.

    Raises ValueError for ``amount < 1``, a self-transfer, or insufficient
    funds — the debit rides ``apply_debit``'s guarded UPDATE, so an
    insufficient balance fails with ZERO writes (no ledger row, no credit).
    Both sides are ledgered: ``transfer_out`` (meta ``{"to": to_id}``) and
    ``transfer_in`` (meta ``{"from": from_id}``). An optional ``memo`` is
    stored verbatim under a ``memo`` key on both rows; callers are responsible
    for trimming/capping it and for escaping at render time. Transfers do NOT
    mint — the booster multiplier is intentionally never applied to the credit,
    so the recipient gets exactly what the sender paid. Rides the caller's
    connection/transaction as the commit boundary.
    """
    if amount < 1:
        raise ValueError("transfer amount must be >= 1")
    if from_id == to_id:
        raise ValueError("cannot transfer to yourself")
    out_meta: dict = {"to": to_id}
    in_meta: dict = {"from": from_id}
    if memo:
        out_meta["memo"] = memo
        in_meta["memo"] = memo
    debited = apply_debit(
        conn, guild_id, from_id, amount, "transfer_out",
        actor_id=from_id, meta=out_meta,
    )
    if not debited:
        raise ValueError("insufficient funds")
    apply_credit(
        conn, guild_id, to_id, amount, "transfer_in",
        actor_id=from_id, meta=in_meta,
    )


def get_ledger(
    conn: sqlite3.Connection, guild_id: int, user_id: int, limit: int = 10
) -> list[sqlite3.Row]:
    """Return the user's most recent ledger rows, newest first."""
    return conn.execute(
        """
        SELECT id, guild_id, user_id, amount, kind, actor_id, meta, created_at
        FROM econ_ledger
        WHERE guild_id = ? AND user_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (guild_id, user_id, limit),
    ).fetchall()


def get_notify_muted(conn: sqlite3.Connection, guild_id: int, user_id: int) -> bool:
    row = conn.execute(
        "SELECT muted FROM econ_notify_prefs WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    return bool(row["muted"]) if row else False


def set_notify_muted(
    conn: sqlite3.Connection, guild_id: int, user_id: int, muted: bool
) -> None:
    conn.execute(
        """
        INSERT INTO econ_notify_prefs (guild_id, user_id, muted)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET muted = excluded.muted
        """,
        (guild_id, user_id, 1 if muted else 0),
    )


# ── faucets: login, conversion, QOTD, game rewards ────────────────────


@dataclass(frozen=True)
class LoginOutcome:
    paid: int
    streak: int
    milestone: int
    grace_consumed: bool
    reset: bool
    shield_consumed: bool = False


def process_login(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    user_id: int,
    *,
    local_day: str,
    source: str,
    booster: bool,
) -> LoginOutcome | None:
    """Pay the daily login for the first qualifying activity of a local day.

    Returns None when the user already logged in this local day. The
    INSERT OR IGNORE on econ_logins is the race anchor: it rides the same
    connection/transaction as the credits, so concurrent triggers pay at
    most once. Milestone bonuses land as a separate "milestone" ledger row.
    """
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO econ_logins (guild_id, user_id, local_day, source, paid)
        VALUES (?, ?, ?, ?, 0)
        """,
        (guild_id, user_id, local_day, source),
    )
    if (cur.rowcount or 0) == 0:
        return None

    row = conn.execute(
        """
        SELECT current_streak, longest_streak, last_login_day, last_grace_day,
               shields
        FROM econ_streaks
        WHERE guild_id = ? AND user_id = ?
        """,
        (guild_id, user_id),
    ).fetchone()
    shields = int(row["shields"]) if row else 0
    ev = logic.evaluate_login(
        today=local_day,
        last_login_day=row["last_login_day"] if row else None,
        current_streak=int(row["current_streak"]) if row else 0,
        last_grace_day=row["last_grace_day"] if row else None,
        shields_held=shields,
    )

    last_grace_day = ev.grace_covers_day if ev.grace_consumed else (
        row["last_grace_day"] if row else None
    )
    longest = max(ev.new_streak, int(row["longest_streak"]) if row else 0)
    if ev.shield_consumed:
        shields = max(0, shields - 1)
    conn.execute(
        """
        INSERT INTO econ_streaks
            (guild_id, user_id, current_streak, longest_streak,
             last_login_day, last_grace_day, shields)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            current_streak = excluded.current_streak,
            longest_streak = excluded.longest_streak,
            last_login_day = excluded.last_login_day,
            last_grace_day = excluded.last_grace_day,
            shields = excluded.shields
        """,
        (
            guild_id, user_id, ev.new_streak, longest, local_day,
            last_grace_day, shields,
        ),
    )

    base = settings.login_voice_base if source == "voice" else settings.login_text_base
    amount = logic.login_amount(ev.new_streak, base, settings.streak_bonus_cap)
    paid = 0
    if amount > 0:
        paid = apply_credit(
            conn,
            guild_id,
            user_id,
            amount,
            "login",
            meta={"local_day": local_day, "source": source, "streak": ev.new_streak},
            booster=booster,
            multiplier=settings.booster_multiplier,
        )

    milestone = logic.milestone_amount(ev.new_streak, settings)
    milestone_paid = 0
    if milestone > 0:
        milestone_paid = apply_credit(
            conn,
            guild_id,
            user_id,
            milestone,
            "milestone",
            meta={"local_day": local_day, "streak": ev.new_streak},
            booster=booster,
            multiplier=settings.booster_multiplier,
        )

    conn.execute(
        """
        UPDATE econ_logins SET paid = ?
        WHERE guild_id = ? AND user_id = ? AND local_day = ?
        """,
        (paid + milestone_paid, guild_id, user_id, local_day),
    )
    return LoginOutcome(
        paid=paid,
        streak=ev.new_streak,
        milestone=milestone_paid,
        grace_consumed=ev.grace_consumed,
        reset=ev.reset,
        shield_consumed=ev.shield_consumed,
    )


def top_up_voice_login(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    user_id: int,
    *,
    local_day: str,
    booster: bool = False,
) -> int:
    """Pay the text→voice difference when voice presence follows a text login.

    The daily login pays whichever source fires first, and text almost always
    wins — a member types before they join a call. Live data (2026-07-23): 688
    text logins against 30 voice ones, so ``login_voice_base`` (15) was paid on
    4% of days while the guide advertised it as the voice rate. Voice presence
    is the signal we most want to reward — it reaches members who never trigger
    command-based faucets — and it was quietly worth a third of list price.

    So a qualifying voice session on a day already claimed by text tops the
    member up by the difference. The streak bonus is deliberately *not*
    recomputed: it was already paid by the text login and rides on whichever
    base won, so the delta is the flat base gap and nothing else.

    Returns the credited amount (0 when there is nothing to do). Exactly-once
    via the UPDATE's ``source = 'text'`` guard: the row flips to 'voice', so a
    replay — or a second voice tick the same day — matches no row and pays
    nothing. Never downgrades voice→text.
    """
    delta = int(settings.login_voice_base) - int(settings.login_text_base)
    if delta <= 0:
        return 0
    cur = conn.execute(
        """
        UPDATE econ_logins SET source = 'voice'
        WHERE guild_id = ? AND user_id = ? AND local_day = ? AND source = 'text'
        """,
        (guild_id, user_id, local_day),
    )
    if (cur.rowcount or 0) == 0:
        return 0
    paid = apply_credit(
        conn,
        guild_id,
        user_id,
        delta,
        "login",
        meta={"local_day": local_day, "source": "voice", "upgrade": True},
        booster=booster,
        multiplier=settings.booster_multiplier,
    )
    conn.execute(
        """
        UPDATE econ_logins SET paid = paid + ?
        WHERE guild_id = ? AND user_id = ? AND local_day = ?
        """,
        (paid, guild_id, user_id, local_day),
    )
    return paid


def purchase_streak_shield(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    user_id: int,
) -> int:
    """Buy the prepaid streak shield (one-shot, held until a reset would land).

    Cap is ONE held at a time. The guarded upsert is the race anchor — it
    claims the slot (``shields = 1`` only where currently 0) before any money
    moves, so two concurrent buys can't both charge; the debit failing then
    unwinds the claim with the caller's transaction. Purchasable at any streak,
    including 0 (it protects the next streak). Returns the price charged.
    Raises ValueError: "not for sale" when the guild has switched the shield
    off on the Shop & Perks page, "already holding" when a shield is held,
    "insufficient" when the debit fails.

    A shield already held keeps working after the switch goes off — it is a
    one-shot the member has already paid for, and burning it to save their
    streak is the whole thing they bought. Switching the line off stops new
    sales; it does not confiscate stock.
    """
    if not settings.shop_streak_shield_enabled:
        raise ValueError("not for sale")
    price = int(settings.price_streak_shield)
    cur = conn.execute(
        """
        INSERT INTO econ_streaks (guild_id, user_id, shields, shield_price)
        VALUES (?, ?, 1, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET shields = 1, shield_price = ?
        WHERE econ_streaks.shields = 0
        """,
        (guild_id, user_id, price, price),
    )
    if (cur.rowcount or 0) == 0:
        raise ValueError("already holding")
    ok = apply_debit(
        conn, guild_id, user_id, price, "streak_shield",
        actor_id=user_id, meta={},
    )
    if not ok:
        raise ValueError("insufficient")
    # shop_purchase quest trigger (one-time setup kind). Deferred import —
    # the quests service imports this module.
    from bot_modules.services.economy_quests_service import (  # noqa: PLC0415
        fire_trigger_inline,
    )

    fire_trigger_inline(conn, guild_id, "shop_purchase", user_id, occurrence="set")
    return price


def _shield_refund_price(row: sqlite3.Row, settings: EconSettings) -> int:
    """The amount to refund for a held shield row.

    A shield bought before migration 114 (``shield_price`` didn't exist yet)
    snapshots as the column default 0 — indistinguishable from "nothing
    held" otherwise. The true historical price is genuinely unrecoverable for
    those, so a stored 0 on a row that IS held (``shields = 1``) falls back
    to the guild's current price rather than silently hiding the refund
    option or zero-crediting it.
    """
    stored = int(row["shield_price"])
    return stored if stored > 0 else int(settings.price_streak_shield)


def get_streak_shield_price(
    conn: sqlite3.Connection, guild_id: int, user_id: int, settings: EconSettings
) -> int:
    """The price to refund for the member's held shield, or 0 if none is held."""
    row = conn.execute(
        "SELECT shield_price FROM econ_streaks "
        "WHERE guild_id = ? AND user_id = ? AND shields = 1",
        (guild_id, user_id),
    ).fetchone()
    return _shield_refund_price(row, settings) if row is not None else 0


def get_streak_shield_status(
    conn: sqlite3.Connection, guild_id: int, user_id: int, settings: EconSettings
) -> tuple[int, int]:
    """(shields held, refund price) in one query — see ``get_streak_shield_price``."""
    row = conn.execute(
        "SELECT shields, shield_price FROM econ_streaks "
        "WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    if row is None:
        return 0, 0
    shields = int(row["shields"])
    return shields, (_shield_refund_price(row, settings) if shields == 1 else 0)


def refund_streak_shield(
    conn: sqlite3.Connection, guild_id: int, user_id: int, settings: EconSettings
) -> int:
    """Refund a held, unconsumed streak shield in full. Returns the amount refunded.

    Refunds the price actually PAID (``shield_price``, snapshotted at
    purchase), not the guild's current price, which may have moved since —
    except for a legacy pre-migration-114 shield, which falls back to the
    current price (see ``_shield_refund_price``). Exactly-once via the
    guarded ``shields = 1`` UPDATE — the same claim-style anchor
    ``purchase_streak_shield`` uses to grab the slot. Raises ValueError("no
    shield held") if nothing is held (already consumed or never bought).
    """
    row = conn.execute(
        "SELECT shield_price FROM econ_streaks WHERE guild_id = ? AND user_id = ? "
        "AND shields = 1",
        (guild_id, user_id),
    ).fetchone()
    if row is None:
        raise ValueError("no shield held")
    price = _shield_refund_price(row, settings)
    cur = conn.execute(
        "UPDATE econ_streaks SET shields = 0, shield_price = 0 "
        "WHERE guild_id = ? AND user_id = ? AND shields = 1",
        (guild_id, user_id),
    )
    if (cur.rowcount or 0) == 0:
        raise ValueError("no shield held")
    if price > 0:
        apply_credit(
            conn, guild_id, user_id, price, "streak_shield_refund",
            actor_id=user_id, meta={},
        )
    return price


def get_streak_summary(
    conn: sqlite3.Connection, guild_id: int, user_id: int, *, today: str | None = None
) -> tuple[int, int]:
    """``(current_streak, longest_streak)`` for a member, zeros if unseen.

    The shield helpers next door read the same row for its ``shields`` column
    only, which left the streak itself with no reader at all — so ``/info``
    could show the shield that protects a streak while having no way to show
    the streak.

    ``current_streak`` is **stored, not live**: it is only ever rewritten by
    ``process_login``, which runs on a message or a voice award. A member who
    stopped posting a week ago still has yesterday's number sitting in the
    column, so reading it verbatim would announce a 12-day streak that the
    member's next message will reset to 1. Pass ``today`` (a local day string
    from ``local_day_for``) and the streak is checked against the very rules a
    login would apply — ``evaluate_login``, unchanged and unduplicated — so a
    streak that cannot survive is reported as zero. Omit ``today`` to get the
    raw stored value.
    """
    row = conn.execute(
        "SELECT current_streak, longest_streak, last_login_day, last_grace_day, "
        "shields FROM econ_streaks WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    if row is None:
        return 0, 0

    current = int(row["current_streak"] or 0)
    longest = int(row["longest_streak"] or 0)
    if today is None or not current:
        return current, longest

    outcome = logic.evaluate_login(
        today=today,
        last_login_day=row["last_login_day"],
        current_streak=current,
        last_grace_day=row["last_grace_day"],
        shields_held=int(row["shields"] or 0),
    )
    # `reset` means a login right now would start over — the run is already
    # gone, whether or not the member has noticed. The personal best stands
    # regardless; that one is history, not a live claim.
    return (0 if outcome.reset else current), longest


def get_streak_shields(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> int:
    """How many streak shields the member holds (0 or 1 under the cap)."""
    row = conn.execute(
        "SELECT shields FROM econ_streaks WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    return int(row["shields"]) if row else 0


def process_conversion(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    user_id: int,
    *,
    local_day: str,
    xp: float,
    booster: bool,
) -> int:
    """Convert one local day's XP to currency; returns the credited amount.

    Idempotent per (guild, user, local_day) via INSERT OR IGNORE on
    econ_conversions — a replayed day returns 0 with no writes. The
    fractional remainder from the latest prior conversion carries in.

    ``conversion_daily_cap`` (0 = none) clips the day's mint. The clip is
    recorded on the conversion row and flagged in the ledger meta, and it
    zeroes the carry — banking the overflow would only postpone it.
    """
    prev = conn.execute(
        """
        SELECT remainder FROM econ_conversions
        WHERE guild_id = ? AND user_id = ?
        ORDER BY local_day DESC LIMIT 1
        """,
        (guild_id, user_id),
    ).fetchone()
    carry = float(prev["remainder"]) if prev else 0.0
    coins, remainder = logic.convert_xp(xp, carry, settings.xp_per_coin)

    cap = max(0, int(settings.conversion_daily_cap))
    clipped = cap > 0 and coins > cap
    if clipped:
        coins, remainder = cap, 0.0

    cur = conn.execute(
        """
        INSERT OR IGNORE INTO econ_conversions
            (guild_id, user_id, local_day, xp, coins, remainder)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (guild_id, user_id, local_day, xp, coins, remainder),
    )
    if (cur.rowcount or 0) == 0:
        return 0
    if coins <= 0:
        return 0
    return apply_credit(
        conn,
        guild_id,
        user_id,
        coins,
        "conversion",
        meta=(
            {"local_day": local_day, "xp": round(xp, 2), "capped": cap}
            if clipped
            else {"local_day": local_day, "xp": round(xp, 2)}
        ),
        booster=booster,
        multiplier=settings.booster_multiplier,
    )


def create_qotd(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    message_id: int,
    question: str,
    posted_by: int,
    local_day: str,
    sponsor_user_id: int = 0,
) -> int:
    """Record a posted QOTD. ``posted_by`` is the mod who ran the command;
    ``sponsor_user_id`` is the member who paid for it (0 for a staff question)
    — deliberately separate columns, since they're different people and both
    matter for an audit.
    """
    cur = conn.execute(
        """
        INSERT INTO econ_qotd
            (guild_id, channel_id, message_id, question, posted_by,
             local_day, created_at, sponsor_user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id,
            channel_id,
            message_id,
            question,
            posted_by,
            local_day,
            time.time(),
            sponsor_user_id,
        ),
    )
    return int(cur.lastrowid or 0)


def qotd_for_message(
    conn: sqlite3.Connection, guild_id: int, message_id: int
) -> sqlite3.Row | None:
    """Return the QOTD registered for this exact message, if any.

    The reward is keyed on the message a member *replied to*, so this is the
    lookup the on_message faucet does — not a channel/day scan. Callers still
    check ``local_day`` themselves: an old QOTD message stays in the table
    forever, and replying to a month of them would otherwise be a coin farm.
    """
    return conn.execute(
        """
        SELECT id, guild_id, channel_id, message_id, question, posted_by,
               local_day, created_at
        FROM econ_qotd
        WHERE guild_id = ? AND message_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (guild_id, message_id),
    ).fetchone()


def try_award_qotd(
    conn: sqlite3.Connection,
    settings: EconSettings,
    qotd_id: int,
    guild_id: int,
    user_id: int,
    *,
    booster: bool,
) -> bool:
    """Pay the QOTD reward once per member; False if already rewarded."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO econ_qotd_rewards (qotd_id, user_id) VALUES (?, ?)",
        (qotd_id, user_id),
    )
    if (cur.rowcount or 0) == 0:
        return False
    if settings.reward_qotd > 0:
        apply_credit(
            conn,
            guild_id,
            user_id,
            settings.reward_qotd,
            "qotd",
            meta={"qotd_id": qotd_id},
            booster=booster,
            multiplier=settings.booster_multiplier,
        )
    return True


def cat_coins_earned_since(
    conn: sqlite3.Connection, guild_id: int, user_id: int, since_ts: float
) -> int:
    """This member's credited ``cat_catch`` coins in one guild since ``since_ts``.

    Feeds ``cat_catch_daily_cap`` (see ``logic.cat_catch_payout``). Scoped four
    ways on purpose — guild, member, kind, and day start — because a cap that
    silently summed the wrong axis would either never bite or bite everyone.

    The total is post-booster, since it reads what was actually credited.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS t FROM econ_ledger "
        "WHERE guild_id = ? AND user_id = ? AND kind = 'cat_catch' "
        "AND created_at >= ?",
        (guild_id, user_id, since_ts),
    ).fetchone()
    return int(row["t"])


def award_game_reward(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    user_id: int,
    *,
    kind: str,
    booster: bool,
) -> int:
    """Credit a game reward; ``kind`` picks the amount. Returns the credit."""
    amounts = {
        "game_participation": settings.reward_game_participation,
        "game_win": settings.reward_game_win,
    }
    if kind not in amounts:
        raise ValueError(f"unknown game reward kind: {kind!r}")
    amount = amounts[kind]
    if amount <= 0:
        return 0
    return apply_credit(
        conn,
        guild_id,
        user_id,
        amount,
        kind,
        booster=booster,
        multiplier=settings.booster_multiplier,
    )


def award_host_bounty(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    host_id: int,
    *,
    joiners: int,
    booster: bool,
) -> int:
    """Credit the host of a finished game, scaled by who turned up.

    ``joiners`` excludes the host. Returns 0 — crediting nothing — when the
    rate is unset (the dark default), when nobody joined, or when the host id
    is unusable. Ledger kind ``game_host``.
    """
    if host_id <= 0:
        return 0
    amount = logic.host_bounty_amount(
        joiners, settings.host_bounty_per_joiner, settings.host_bounty_cap
    )
    if amount <= 0:
        return 0
    return apply_credit(
        conn,
        guild_id,
        host_id,
        amount,
        "game_host",
        meta={"joiners": joiners},
        booster=booster,
        multiplier=settings.booster_multiplier,
    )


def member_is_booster(bot: discord.Client, guild_id: int, user_id: int) -> bool:
    """True when the member is currently boosting the guild."""
    guild = bot.get_guild(guild_id)
    if guild is None:
        return False
    member = guild.get_member(user_id)
    return member is not None and member.premium_since is not None


@dataclass(frozen=True)
class DmDelivery:
    """What happened to one economy notification, and *where* it landed.

    ``notify_member`` collapses this to a bool for its ~20 callers, none of
    which care. The login digest does care: it edits its message in place all
    day, so it needs the ``discord.Message`` back — and it needs to know the
    message is a DM rather than the public bank-channel fallback, because that
    fallback is a deliberately different embed (the DM form carries the
    wellness section, which must never be posted publicly). Editing the wrong
    surface would be a privacy leak, so the surface is recorded explicitly
    rather than inferred from a truthy return.

    ``dropped`` is the trap this type exists to close: a muted member, a member
    without the opt-in game role, and a failed DM with the public fallback
    switched off all count as *handled* — the old bool said ``True`` and no
    message existed. Only ``surface == "dm"`` yields a ``message``.
    """

    surface: str  # "dm" | "bank" | "dropped" | "failed"
    message: discord.Message | None = None

    @property
    def delivered(self) -> bool:
        """The legacy bool: False only when both the DM and the fallback failed."""
        return self.surface != "failed"


async def deliver_econ_dm(
    bot: discord.Client,
    db_path: Path,
    guild_id: int,
    user_id: int,
    *,
    embed: discord.Embed | None = None,
    content: str | None = None,
    require_game_role: bool = False,
    fallback_embed: discord.Embed | None = None,
    public_fallback: bool = True,
) -> DmDelivery:
    """DM an economy notification, falling back to the bank channel.

    The delivery core behind :func:`notify_member`, which is the same thing
    narrowed to a bool. Callers that need the sent ``discord.Message`` back —
    only the login digest, which edits its card in place as quest progress
    lands — call this and check ``surface == "dm"`` before storing a handle.

    A muted member (econ_notify_prefs) is silently dropped and counts as
    delivered. The result is ``failed`` only when both the DM and the
    bank-channel fallback fail.

    ``fallback_embed``, when given, replaces ``embed`` on the public
    bank-channel fallback — for embeds whose DM form carries fields that
    must never be posted publicly (e.g. the login digest's wellness
    section).

    ``public_fallback=False`` turns the bank-channel fallback off, so a
    failed DM is dropped and counts as handled (like a mute). Recurring
    opt-in notices pass it: a member who has shut their DMs to the bot has
    already said how much bot contact they want, and answering that by
    publishing their streak and quest progress in a public channel with a
    ping is louder, not quieter. Transactional notices keep the fallback —
    the member needs those to arrive.

    ``require_game_role`` gates the notice on the opt-in economy role: a
    member without it is dropped silently (returns True, like a mute) so
    recurring engagement notices — streaks, milestones — only reach players
    who opted in. With no ``game_role_id`` configured, nobody has opted in
    yet, so the gate defaults to dropping everyone rather than notifying the
    whole guild. Leave it False for transactional notices (e.g. rental
    billing) that target a member by their prior spend, not by opt-in.
    """
    import discord  # local import to keep this module import-light for tests

    from bot_modules.core.db_utils import open_db

    def _read():
        with open_db(db_path) as conn:
            return (
                get_notify_muted(conn, guild_id, user_id),
                load_econ_settings(conn, guild_id),
            )

    muted, settings = await asyncio.to_thread(_read)
    if muted:
        return DmDelivery("dropped")

    guild = bot.get_guild(guild_id)
    member = guild.get_member(user_id) if guild else None

    if require_game_role:
        if (
            not settings.game_role_id
            or member is None
            or not any(r.id == settings.game_role_id for r in member.roles)
        ):
            return DmDelivery("dropped")

    if embed is not None:
        # Branded here rather than at the ~20 call sites, so every economy
        # notice inherits the guild's accent and attribution while this
        # function keeps sole ownership of the delivery policy above.
        # The bank-channel fallback below reuses the same embed: the server
        # name in its own channel is redundant but harmless, and the accent
        # is right either way.
        from bot_modules.services.dm_branding import (
            brand_dm_embed,
            guild_display_name,
            guild_icon_url,
            resolve_dm_accent,
        )

        brand_dm_embed(
            embed,
            guild_name=guild_display_name(guild),
            guild_icon_url=guild_icon_url(guild),
            color=await resolve_dm_accent(db_path, guild),
        )

    kwargs: dict = {}
    if content:
        kwargs["content"] = content
    if embed:
        kwargs["embed"] = embed

    if member is not None:
        try:
            sent = await member.send(**kwargs)
        except (discord.Forbidden, discord.HTTPException):
            pass
        else:
            return DmDelivery("dm", sent)

    if not public_fallback:
        return DmDelivery("dropped")
    if guild is None or not settings.bank_channel_id:
        return DmDelivery("failed")
    channel = guild.get_channel(settings.bank_channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        return DmDelivery("failed")
    mention = f"<@{user_id}>"
    fallback_kwargs: dict = {"content": f"{mention} {content}" if content else mention}
    # This posts into the public bank channel and some callers pass raw
    # member-authored bodies (pin message, sponsor question). Restrict pings to
    # the target member only so an embedded @everyone / role / other-user
    # mention in that body can't fire. Sinks every caller at once.
    fallback_kwargs["allowed_mentions"] = discord.AllowedMentions(
        users=[discord.Object(id=user_id)], everyone=False, roles=False
    )
    if fallback_embed is not None:
        fallback_kwargs["embed"] = fallback_embed
    elif embed:
        fallback_kwargs["embed"] = embed
    try:
        posted = await channel.send(**fallback_kwargs)
    except (discord.Forbidden, discord.HTTPException):
        return DmDelivery("failed")
    return DmDelivery("bank", posted)


async def notify_member(
    bot: discord.Client,
    db_path: Path,
    guild_id: int,
    user_id: int,
    *,
    embed: discord.Embed | None = None,
    content: str | None = None,
    require_game_role: bool = False,
    fallback_embed: discord.Embed | None = None,
    public_fallback: bool = True,
) -> bool:
    """DM an economy notification — :func:`deliver_econ_dm` narrowed to a bool.

    Returns False only when both the DM and the bank-channel fallback fail; a
    muted or non-opted-in member counts as delivered. Every caller but the
    login digest wants exactly this, so the delivery policy stays in one place
    and no call site had to change when the digest needed the message back.
    """
    result = await deliver_econ_dm(
        bot, db_path, guild_id, user_id,
        embed=embed, content=content,
        require_game_role=require_game_role,
        fallback_embed=fallback_embed,
        public_fallback=public_fallback,
    )
    return result.delivered


# ── legal-erasure sweep ────────────────────────────────────────────────


# Per-member economy/casino state removed by a legal erasure
# (``privacy_service.purge_user_data``). ``econ_ledger`` is deliberately
# absent: it is the pseudonymous financial record, and deleting one side of
# transfers/rakes would break double-entry audit sums and every baseline
# report. Round/draw history keyed by winner (drops, bounties, raffle draws,
# auctions, qotd) is likewise kept as game history.
#
# Maintenance rule (docs/data_register.md): a new econ_*/
# casino_* table with per-member rows either joins this list or documents why
# it is preserved.
_PURGE_USER_ID_TABLES: tuple[str, ...] = (
    "econ_wallets",
    "econ_streaks",
    "econ_logins",
    "econ_notify_prefs",
    "econ_conversions",
    "econ_quest_claims",
    "econ_quest_progress",
    "econ_quest_progress_marks",
    "econ_rerolls",
    "econ_set_bonus",
    "econ_board_overrides",
    "econ_kind_activity",
    "econ_kind_activity_occ",
    "econ_setup_marks",
    "econ_login_digest_cards",
    "econ_onboarding_dms",
    "econ_personal_roles",
    "econ_rentals",
    "econ_vouchers",
    "econ_raffle_tickets",
    "econ_photo_rewards",
    "econ_intake_rewards",
    "econ_qotd_rewards",
    "econ_pin_submissions",
    "econ_theme_submissions",
    "econ_shop_purchases",
    "econ_community_contrib",
    "econ_community_tier_payouts",
    "econ_game_wagers",
    "econ_bounty_contributions",
    "econ_auction_bids",
    "casino_daily",
    "casino_weekly",
    "casino_member_stats",
    "casino_daily_net",
    "casino_ticker",
    "casino_blackjack_hands",
    "casino_war_hands",
    "casino_mines_hands",
    "casino_race_bets",
    "casino_keno_bets",
    "casino_roulette_bets",
    "casino_baccarat_bets",
    "casino_dice_bets",
    "casino_pools_bets",
    # Since migration 158 a round names the player who opened it, so the
    # rounds tables identify a member the same way their bets do and have
    # to be erased alongside them. Pools is absent on purpose: its round is
    # a shared daily market with no owner (its per-member data is entirely
    # in casino_pools_bets, above).
    "casino_roulette_rounds",
    "casino_race_rounds",
    "casino_baccarat_rounds",
    "casino_dice_rounds",
    "casino_keno_rounds",
)


def econ_purge_user(conn: sqlite3.Connection, guild_id: int, user_id: int) -> None:
    """Delete every per-member economy/casino row for *user_id* in *guild_id*.

    Missing tables are tolerated (guild deployments differ in age); a failed
    table logs and the sweep continues, matching ``purge_user_data``'s
    schema-tolerance contract.
    """
    import logging

    log = logging.getLogger("dungeonkeeper.economy")
    # Open shop orders are settled BEFORE the rows are deleted. A pending order
    # holds two things outside its own table: a unit of the item's stock, and a
    # todo on the mods' board pointing back at it. Deleting the row alone would
    # strand the todo — a mod ticks it off and silently delivers nothing — and
    # burn the stock unit forever. Erasure is not a reason to keep someone
    # else's shelf short. See docs/data_register.md.
    try:
        from bot_modules.services.economy_shop_items_service import (  # noqa: PLC0415
            release_open_orders,
        )

        release_open_orders(conn, guild_id, user_id)
    except sqlite3.Error as exc:
        log.warning(
            "econ purge: failed releasing shop orders for user %d in guild %d: %s",
            user_id, guild_id, exc,
        )
    # A RUNNING flash theme is detached rather than deleted, for the same
    # reason: it holds a pinned announcement that only the expiry sweep knows
    # how to take down, and the sweep finds its work by reading live rows.
    # Deleting it would strip the name and strand the pin permanently. The
    # member's other theme rows are deleted by the sweep below.
    try:
        from bot_modules.services.economy_theme_service import (  # noqa: PLC0415
            anonymise_live_theme,
        )

        anonymise_live_theme(conn, guild_id, user_id)
    except sqlite3.Error as exc:
        log.warning(
            "econ purge: failed detaching live theme for user %d in guild %d: %s",
            user_id, guild_id, exc,
        )
    for table in _PURGE_USER_ID_TABLES:
        try:
            conn.execute(
                f"DELETE FROM {table} WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
        except sqlite3.Error as exc:
            log.warning(
                "econ purge: failed on %s for user %d in guild %d: %s",
                table, user_id, guild_id, exc,
            )
    # Reply-credit rows name the member on either side.
    for col in ("target_author_id", "replier_id"):
        try:
            conn.execute(
                f"DELETE FROM econ_msg_replies WHERE guild_id = ? AND {col} = ?",
                (guild_id, user_id),
            )
        except sqlite3.Error as exc:
            log.warning(
                "econ purge: failed on econ_msg_replies.%s for user %d in guild %d: %s",
                col, user_id, guild_id, exc,
            )
