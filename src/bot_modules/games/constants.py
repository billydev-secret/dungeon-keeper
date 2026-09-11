from bot_modules.games_rushmore.logic import DEFAULT_PICK_SECONDS
from bot_modules.services.embeds import COLOR_GREEN, COLOR_RED

BRAND_COLOR = 0xDAA520  # Goldenrod
WARNING_COLOR = 0xFF6B35
SUCCESS_COLOR = COLOR_GREEN  # canonical semantic success green (services.embeds)
ERROR_COLOR = COLOR_RED      # canonical semantic danger red (services.embeds)

# Game phase colors — used consistently across all cogs
PHASE_JOINING  = 0xDAA520   # goldenrod  — lobby / join
PHASE_PLAYING  = 0x4E9AF1   # blue       — active round
PHASE_RESULTS  = 0x57F287   # green      — round results
PHASE_RECAP    = 0xB8860B   # dark gold  — final recap / game over

GAME_ICONS = {
    'ffa': '🎭',
    'ffa_banner': '🃏',
    # 'photo' is intentionally absent — Photo Challenge left the games menu and
    # the /games help list (it's scheduled-only now). GAME_NAMES keeps its
    # display name for logs/scheduler lookups. 'mahjong' is absent for the
    # same reason: it is a channel-native table (listed under Rooms & Tables
    # by the help cog), not a /games play launch, and its settled hands are
    # self-recorded history rows that only need a display name.
    'traditional': '🎲',
    'compliment': '💛',
    'mfk': '💍',
    'wyr': '🤔',
    'nhie': '⛔',
    'mlt': '👑',
    'ttl': '🤥',
    'hottakes': '🔥',
    'story': '📖',

    'ama': '🎙️',
    'fantasies': '✨',
    'price': '💰',
    'rushmore': '🗿',
    'clapback': '⚔️',
    'legitlibs': '📝',

    # Duel / lobby games (real-time challenge or elimination). Keyed by each
    # game's GAME_KEY so audit- and session-log icon lookups resolve too. They
    # show in /games help alongside the party games but are NOT party games —
    # DUEL_GAME_KEYS below keeps them out of the advertised party-game count.
    'pressure': '♨️',
    'quickdraw': '🤠',
    'chicken': '🐔',
    'hot_potato': '💣',
    'hot_potato_group': '🧨',
    'musical_chairs': '🪑',
    'risky_roll': '🎰',
}

# Games in GAME_ICONS that are duels/lobby/standalone rather than party games.
# docs/features.md and the web manual advertise a party-game *count* in prose;
# these keys are excluded from it (see the tripwires in
# tests/test_games_help_logic.py).
DUEL_GAME_KEYS = frozenset({
    'pressure', 'quickdraw', 'chicken', 'hot_potato', 'hot_potato_group',
    'musical_chairs', 'risky_roll',
})

GAME_NAMES = {
    'ffa': 'Anonymous Truth or Dare',
    'ffa_banner': 'Truth or Dare Card',
    'photo': 'Photo Challenge',
    'mahjong': 'Meadow Mahjong',
    'traditional': 'Truth or Dare',
    'compliment': 'Spin the Compliment',
    'mfk': 'Marry, Fornicate, Kiss',
    'wyr': 'Would You Rather',
    'nhie': 'Never Have I Ever',
    'mlt': 'Most Likely To',
    'ttl': 'Two Truths and a Lie',
    'hottakes': 'Hot Takes',
    'story': 'Story Builder',

    'ama': 'Anonymous AMA',
    'fantasies': 'Fantasies & Dealbreakers',
    'price': 'Name Your Price',
    'rushmore': 'Mt. Rushmore Draft',
    'clapback': 'Clapback',
    'legitlibs': 'LegitLibs',
    'pressure': 'Pressure Cooker',
    'quickdraw': 'Quickdraw',
    'chicken': 'Chicken',
    'hot_potato': 'Hot Potato',
    'hot_potato_group': 'Hot Potato (Group)',
    'musical_chairs': 'Musical Chairs',
    'risky_roll': 'Risky Rolls',
}

# ── Scheduling registry ─────────────────────────────────────────────────────
# Party games that can be auto-launched by the scheduler (web dashboard).
# PvP / duel / lobby-challenge games (e.g. 'pressure', Quickdraw, Chicken,
# Hot Potato, Musical Chairs) are intentionally excluded — they need a live
# challenge/opponent flow. Adding a game here REQUIRES registering a launcher
# in its cog setup() (see bot.game_launchers); the startup coverage check warns
# on drift.
# NOTE: 'photo' is intentionally NOT here — Photo Challenge is its own
# standalone dashboard feature (/api/photo-challenge, own channel + schedule),
# not part of the shared games menu/scheduler. Its schedule rows still ride
# the games_scheduled table + this loop (game-type-agnostic), but they're
# created via the dedicated routes and hidden from the shared scheduler UI.
SCHEDULABLE_GAME_TYPES = [
    'ffa', 'ffa_banner', 'traditional', 'compliment', 'mfk', 'wyr', 'nhie', 'mlt', 'ttl',
    'hottakes', 'story', 'ama', 'fantasies', 'price', 'rushmore', 'clapback',
    'legitlibs', 'risky_roll',
]

# ── Lobby registry ──────────────────────────────────────────────────────────
# Party games that open a join lobby and wait for a human to press the start
# button. Only these support the `start_in` countdown and the "time to start"
# host nudge — the other party games post their first prompt the instant the
# command runs, so there's no button for a countdown to point at. Hot Takes
# (a submit lobby waiting on Start Voting) and Fantasies (a control panel
# waiting on the first Start Round) joined on 2026-09-04 (anon-tail-72): an
# empty Hot Takes lobby had no exit but the 24h sweep. Name Your Price joined
# the same day (trivia-tail-85): it used to start with no lobby and no ping,
# and its rounds could never close early because nobody knew the roster.
LOBBY_GAME_TYPES = frozenset({
    'clapback', 'compliment', 'fantasies', 'hottakes', 'mfk', 'mlt', 'price', 'rushmore', 'story',
})

# The literal label on each lobby game's start button, so the host nudge can
# name the button they're actually looking at. Keys must match
# LOBBY_GAME_TYPES (tested in tests/test_game_start_ping_service.py).
LOBBY_START_BUTTON = {
    'clapback': 'Start',
    'compliment': 'Close & Generate',
    'fantasies': 'Start Round',
    'hottakes': 'Start Voting',
    'mfk': 'Close & Assign',
    'mlt': 'Start',
    'price': 'Start',
    'rushmore': 'Start Draft',
    'story': 'Start Story',
}

# The smallest roster each lobby game's start button accepts, mirrored from
# the cogs (clapback / mlt / rushmore logic ``MIN_PLAYERS``; compliment and
# story refuse below 2, mfk below 4). The idle-lobby sweep
# (``game_start_ping_service``) cancels a lobby that has sat this far below
# its floor for the configured hour — a lobby that *could* start is left to
# its host. mlt and rushmore may raise their floor per game in the payload;
# the sweep reads that first. Keys must match LOBBY_GAME_TYPES. Hot Takes'
# roster is its distinct submitters (``participants``) and its Start Voting
# needs two takes (``games_hottakes.logic.MIN_TAKES``); Fantasies has no
# join at all, so its floor is one — an unstarted panel with nobody in it
# is what the hour closes. Name Your Price starts at two (``games_price.logic
# .MIN_PLAYERS``): a round needs two prices to compare, and the vote's own
# floor of three is applied per round, not at the door.
LOBBY_MIN_PLAYERS = {
    'clapback': 3,
    'compliment': 2,
    'fantasies': 1,
    'hottakes': 2,
    'mfk': 4,
    'mlt': 3,
    'price': 2,
    'rushmore': 3,
    'story': 2,
}

# ── Hosting registry ────────────────────────────────────────────────────────
# What the scheduler UI tells an admin about each schedulable game, because
# "schedule it" reads as "it runs itself" and for most games it doesn't
# (discovery-4): a scheduled Clapback posts a lobby and waits for a press.
#
# SELF_RUNNING: posts and finishes with nobody at the keyboard — a prompt card
# (ffa / ffa_banner), Risky Rolls' timed round, the daily photo card.
# TIMER_RUNNING: self-run only when the schedule (or the game's dashboard
# dial) sets a round timer — a scheduled WYR / NHIE then auto-advances and
# ends at its round cap, and any voter may press Next; host-paced, it shows
# one question and waits. Everything else opens a lobby or a host-driven
# round and needs a human. Disjoint from LOBBY_GAME_TYPES by construction
# (tested in tests/web/test_scheduled_games_routes.py).
SELF_RUNNING_GAME_TYPES = frozenset({'ffa', 'ffa_banner', 'risky_roll', 'photo'})
TIMER_RUNNING_GAME_TYPES = frozenset({'wyr', 'nhie'})
# AUTO_START_LOBBY: a lobby whose cog registers a countdown starter in
# ``bot.lobby_auto_starters`` (clapback-8). A scheduled row stamps a
# 10-minute countdown and the start-ping sweep starts the game itself once
# the floor has joined, so the scheduler must not call it "Needs a host"
# (P4 leftover, 2026-09-04). A subset of LOBBY_GAME_TYPES by construction,
# and the cogs' registrations are mirrored by test
# (tests/test_games_help_logic.py).
AUTO_START_LOBBY_TYPES = frozenset({'clapback', 'price'})


def hosting_kind(game_type: str) -> str:
    """``'self'`` / ``'timer'`` / ``'countdown'`` / ``'host'`` — the registry above."""
    if game_type in SELF_RUNNING_GAME_TYPES:
        return 'self'
    if game_type in TIMER_RUNNING_GAME_TYPES:
        return 'timer'
    if game_type in AUTO_START_LOBBY_TYPES:
        return 'countdown'
    return 'host'


# The label each hosting kind wears on the scheduler page, the /games help
# panel and the /games play descriptions. games-scheduling.js carries the
# same four keys.
HOSTING_LABEL = {
    'self': 'Self-running',
    'timer': 'Self-running with a round timer',
    'countdown': 'Starts itself after a countdown',
    'host': 'Needs a host',
}

# ── Play registry ───────────────────────────────────────────────────────────
# The two facts a member needs before picking a game — how many people it
# takes and whether somebody has to run it — plus a rough length, keyed by
# every GAME_ICONS entry. ``play_description`` renders them into the ≤100-char
# line each ``/games play`` subcommand shows in Discord's picker
# (discovery-13) and the /games help panel reads the same rows, so the two
# surfaces can't drift apart. Floors mirror the cogs: LOBBY_MIN_PLAYERS for
# the lobby games, ``games_ttl.logic.MIN_PLAYERS`` (three — Start Guessing
# refuses fewer, tested in tests/test_games_help_logic.py), the duel rosters,
# Musical Chairs' default lobby.
GAME_MIN_PLAYERS = {
    'ffa': 1,
    'ffa_banner': 1,
    'traditional': 2,
    'compliment': 2,
    'mfk': 4,
    'wyr': 2,
    'nhie': 2,
    'mlt': 3,
    'ttl': 3,
    'hottakes': 2,
    'story': 2,
    'ama': 2,
    'fantasies': 2,
    'price': 2,
    'rushmore': 3,
    'clapback': 3,
    'legitlibs': 2,
    'pressure': 2,
    'quickdraw': 2,
    'chicken': 2,
    'hot_potato': 2,
    'hot_potato_group': 3,
    'musical_chairs': 3,
    'risky_roll': 2,
}

# Rough wall-clock length at the default settings, for the help panel only.
GAME_TYPICAL_LENGTH = {
    'ffa': 'Open-ended — the host closes it',
    'ffa_banner': 'One card',
    'traditional': '20–40 min',
    'compliment': '~15 min',
    'mfk': '~10 min',
    'wyr': '~10 min (10 rounds)',
    'nhie': '~10 min (10 rounds)',
    'mlt': '~10 min (10 rounds)',
    'ttl': '~15 min',
    'hottakes': '~15 min',
    'story': '~20 min (10 sentences)',
    'ama': '30–60 min',
    'fantasies': '~20 min',
    'price': '~15 min (5 rounds)',
    'rushmore': '~15 min',
    'clapback': '~20 min (5 rounds)',
    'legitlibs': '~5 min a story',
    'pressure': '~5 min',
    'quickdraw': '~2 min',
    'chicken': '~5 min',
    'hot_potato': '~3 min',
    'hot_potato_group': '~5 min',
    'musical_chairs': '~10 min',
    'risky_roll': 'Open up to 2 h, then one question',
}

# Pacing where the kind-derived phrase below would be wrong or vague: a
# challenge or a duel lobby plays itself out once accepted, and the host-paced
# games that wait on a named button outside LOBBY_GAME_TYPES name it here.
_PLAY_PACING_OVERRIDE = {
    'traditional': 'opt in, then the host presses Ask Question',
    'ttl': 'submit statements, then the host presses Start Guessing',
    'ama': 'volunteer to answer; the host ends it',
    'legitlibs': 'join lobby, then the host presses Start',
    'pressure': 'challenge a member; plays itself out',
    'quickdraw': 'challenge a member; plays itself out',
    'hot_potato': 'challenge a member; plays itself out',
    'chicken': 'duel or lobby; runs itself once started',
    'hot_potato_group': 'lobby; runs itself once started',
    'musical_chairs': 'lobby; runs itself once started',
}


def play_pacing(game_type: str) -> str:
    """One clause on who keeps the game moving, from the hosting registry."""
    override = _PLAY_PACING_OVERRIDE.get(game_type)
    if override:
        return override
    kind = hosting_kind(game_type)
    if kind == 'self':
        return 'runs itself once posted'
    if kind == 'timer':
        return 'host presses Next each round, or set a round timer'
    if kind == 'countdown':
        return "join lobby; starts on a countdown or the host's Start"
    button = LOBBY_START_BUTTON.get(game_type)
    if button:
        return f'join lobby, then the host presses {button}'
    return 'the host runs each round'


def play_floor(game_type: str) -> str:
    """``'3+ players'`` — or ``'any number of players'`` for a prompt card."""
    floor = GAME_MIN_PLAYERS.get(game_type, 1)
    return 'any number of players' if floor <= 1 else f'{floor}+ players'


def play_description(game_type: str) -> str:
    """The ``/games play <game>`` picker line: name, floor and pacing, ≤100 chars
    (Discord's limit on a command description — tested)."""
    name = GAME_NAMES.get(game_type, game_type)
    return f'{name} — {play_floor(game_type)}, {play_pacing(game_type)}'

# Some schedulable types are display variants of a base game — they share the
# question bank, history rows, and the base game's enable/disable toggle. Map a
# variant to its base so a single games-config toggle governs both. Used by the
# scheduler's enable check (see scheduled_games_service).
SCHEDULE_BASE_GAME_TYPE = {
    'ffa_banner': 'ffa',
}

# Per-game option fields the scheduler/web UI can collect. Each field:
#   name    — key in the options dict passed to launch()
#   label   — UI label
#   type    — 'str' | 'int' | 'bool' | 'choice'
#   default — default value when omitted
#   (int)   — optional 'min'/'max'
#   (choice)— 'choices': list of {'value', 'label'}
# Games with no setup options have an empty list. Mirrors each cog's slash params.
SCHEDULE_OPTION_SCHEMA = {
    'ffa': [
        {'name': 'kind', 'label': 'Prompt type', 'type': 'choice', 'default': 'random',
         'choices': [{'value': 'random', 'label': 'Random'},
                     {'value': 'truth', 'label': 'Truth'},
                     {'value': 'dare', 'label': 'Dare'}]},
        {'name': 'prompt', 'label': 'Custom prompt (optional)', 'type': 'str', 'default': ''},
    ],
    'ffa_banner': [
        {'name': 'kind', 'label': 'Prompt type', 'type': 'choice', 'default': 'random',
         'choices': [{'value': 'random', 'label': 'Random'},
                     {'value': 'truth', 'label': 'Truth'},
                     {'value': 'dare', 'label': 'Dare'}]},
        {'name': 'prompt', 'label': 'Custom prompt (optional)', 'type': 'str', 'default': ''},
    ],
    'traditional': [
        {'name': 'single_choice', 'label': 'One category per player (radio-style)',
         'type': 'bool', 'default': False},
    ],
    'compliment': [],
    'mfk': [
        {'name': 'options', 'label': 'Custom categories (comma-separated, optional)',
         'type': 'str', 'default': ''},
    ],
    'wyr': [
        {'name': 'question', 'label': "Opening question ('option A | option B', optional)",
         'type': 'str', 'default': ''},
        {'name': 'round_seconds', 'label': 'Seconds per round (0 = host presses Next)', 'type': 'int',
         'default': 0, 'min': 0, 'max': 300},
        {'name': 'max_rounds', 'label': 'Rounds before the recap (0 = until ended)', 'type': 'int',
         'default': 10, 'min': 0, 'max': 50},
    ],
    'nhie': [
        {'name': 'question', 'label': 'Opening statement (optional)', 'type': 'str', 'default': ''},
        {'name': 'lives', 'label': 'Lives (0 = no elimination)', 'type': 'int',
         'default': 3, 'min': 0, 'max': 10},
        {'name': 'round_seconds', 'label': 'Seconds per round (0 = host presses Next)', 'type': 'int',
         'default': 0, 'min': 0, 'max': 300},
        {'name': 'max_rounds', 'label': 'Rounds before the recap (0 = until ended)', 'type': 'int',
         'default': 10, 'min': 0, 'max': 50},
    ],
    'mlt': [
        {'name': 'question', 'label': 'Opening prompt (optional)', 'type': 'str', 'default': ''},
        {'name': 'round_seconds', 'label': 'Seconds per round (0 = host presses Next)', 'type': 'int',
         'default': 0, 'min': 0, 'max': 300},
        {'name': 'max_rounds', 'label': 'Rounds before the recap (0 = until ended)', 'type': 'int',
         'default': 10, 'min': 0, 'max': 50},
    ],
    'ttl': [
        {'name': 'prompt', 'label': 'Theme/prompt (optional)', 'type': 'str', 'default': ''},
        {'name': 'vote_timer', 'label': 'Vote seconds (0 = host advances)', 'type': 'int',
         'default': 0, 'min': 0, 'max': 300},
    ],
    'hottakes': [],
    'story': [
        {'name': 'max_sentences', 'label': 'Max sentences', 'type': 'int',
         'default': 10, 'min': 1, 'max': 30},
        {'name': 'visibility', 'label': 'Visibility', 'type': 'choice', 'default': 'blind',
         'choices': [{'value': 'blind', 'label': 'Blind (prev sentence only)'},
                     {'value': 'full', 'label': 'Full (whole story)'}]},
        {'name': 'starter', 'label': 'Starter sentence (optional)', 'type': 'str', 'default': ''},
    ],
    'ama': [
        {'name': 'mode', 'label': 'Mode', 'type': 'choice', 'default': 'unfiltered',
         'choices': [{'value': 'unfiltered', 'label': 'Unfiltered (post immediately)'},
                     {'value': 'screened', 'label': 'Screened (host approves)'}]},
        {'name': 'format', 'label': 'Format', 'type': 'choice', 'default': 'hot_seat',
         'choices': [{'value': 'hot_seat', 'label': 'Hot Seat (one at a time)'},
                     {'value': 'panel', 'label': 'Open Panel (ask anyone opted in)'}]},
    ],
    'fantasies': [],
    'price': [
        {'name': 'rounds', 'label': 'Rounds', 'type': 'int', 'default': 5, 'min': 1, 'max': 20},
        {'name': 'timer', 'label': 'Submission seconds/round', 'type': 'int', 'default': 30},
        {'name': 'vote_timer', 'label': 'Voting seconds/round', 'type': 'int', 'default': 20},
        # 'ai' was retired with the Prompts & AI studios (trivia-tail-94); a
        # scheduled game with nobody at the keyboard runs on the bank.
        {'name': 'source', 'label': 'Scenario source', 'type': 'choice', 'default': 'bank',
         'choices': [{'value': 'bank', 'label': 'Question bank'},
                     {'value': 'host', 'label': 'Host writes'},
                     {'value': 'players', 'label': 'Players submit'}]},
    ],
    'rushmore': [
        {'name': 'topic', 'label': 'Topic (optional)', 'type': 'str', 'default': ''},
        # Not a literal: this schema is what the Scheduling and Feature
        # Rotation forms submit, and a value in the submitted options
        # beats both the per-guild dial and the code default. Restating
        # the number here once left scheduled drafts on the old 30s
        # while member-started ones moved to 45.
        {'name': 'timer', 'label': 'Pick seconds', 'type': 'int',
         'default': DEFAULT_PICK_SECONDS},
        {'name': 'vote_timer', 'label': 'Voting seconds', 'type': 'int', 'default': 30},
        {'name': 'source', 'label': 'Topic source', 'type': 'choice', 'default': 'host',
         'choices': [{'value': 'host', 'label': 'Host writes'},
                     {'value': 'ai', 'label': 'AI generated'},
                     {'value': 'bank', 'label': 'Question bank'}]},
        {'name': 'mode', 'label': 'Draft mode', 'type': 'choice', 'default': 'snake',
         'choices': [{'value': 'snake', 'label': 'Snake draft (one at a time)'},
                     {'value': 'blitz', 'label': 'Blitz (everyone picks at once)'}]},
    ],
    'clapback': [
        {'name': 'rounds', 'label': 'Rounds', 'type': 'int', 'default': 5, 'min': 1, 'max': 15},
        {'name': 'timer', 'label': 'Answer seconds', 'type': 'int', 'default': 120, 'min': 15, 'max': 180},
        {'name': 'vote_timer', 'label': 'Vote seconds/matchup', 'type': 'int', 'default': 40, 'min': 10, 'max': 60},
        {'name': 'anonymous', 'label': 'Hide authors until recap', 'type': 'bool', 'default': False},
        {'name': 'tags', 'label': 'Prompt tags (comma-separated, optional)', 'type': 'str', 'default': ''},
    ],
    'legitlibs': [
        {'name': 'mode', 'label': 'Mode', 'type': 'choice', 'default': 'classic',
         'choices': [{'value': 'classic', 'label': 'Classic (sequential fill)'},
                     {'value': 'quiplash', 'label': 'Quiplash (all fill, all revealed)'}]},
        {'name': 'tier', 'label': 'Heat tier (1-4)', 'type': 'int', 'default': 2, 'min': 1, 'max': 4},
    ],
    'risky_roll': [
        {'name': 'auto_close_players', 'label': 'Auto-close after N players roll', 'type': 'int',
         'default': 25, 'min': 2, 'max': 100},
        {'name': 'auto_close_minutes', 'label': 'Auto-close after N minutes', 'type': 'int',
         'default': 120, 'min': 1, 'max': 1440},
    ],
}

HOW_TO_PLAY = {
    'ffa': (
        "🎭 **Truth or Dare**\n"
        "The host drops a Truth or Dare prompt in two flavors:\n\n"
        "• **/games play ffa** — posts an embed with anonymous reply buttons; "
        "replies land back in the channel and a live counter tracks them:\n"
        "   • **🎭 Reply Anonymously** — same anon nickname the whole time\n"
        "   • **🎲 Reply as Someone New** — a fresh nickname each time\n"
        "   Replies are posted with no attribution (mods can still see who sent them).\n"
        "   The host presses **Next** for another prompt and **Close** to end the game — "
        "that posts a recap of which prompts drew the most replies and pays everyone who replied.\n"
        "• **/games play ffa_banner** — just drops the prompt card in the channel "
        "for open discussion (no anonymous replies)\n\n"
        "💡 The reply-button game serves **truths** unless the host picks `kind:dare` "
        "(most dares want a voice note or a photo, which an anonymous text box can't do); "
        "the banner card picks either. The host can also write their own prompt, or schedule "
        "an automated series from the dashboard. Spicier (NSFW) prompts appear only in "
        "channels marked age-restricted in Discord."
    ),
    'traditional': (
        "🎲 **Truth or Dare**\n"
        "Classic Truth or Dare, but everyone picks what they're up for.\n\n"
        "1. Click the categories you want to opt into: **SFW Truth**, **SFW Dare**, "
        "**NSFW Truth**, **NSFW Dare** — pick as many as you like\n"
        "2. The host clicks **Ask Question**: the bot picks a player from the pool "
        "and opens the question box already filled in from the question bank — "
        "send it, edit it, or type over it\n"
        "3. **Write My Own** does the same with an empty box; **Bank Round** deals "
        "everyone a bank question at once\n"
        "4. Each player gets one question per category they opted into — once "
        "everyone's been asked, the next **Ask Question** starts another pass\n\n"
        "💡 Players who haven't been asked yet are picked first to keep things fair.\n"
        "💡 **End Game** posts the recap and pays everyone who played. A game with "
        "nothing pressed for a while ends itself the same way.\n"
        "💡 Host tip: pass **single_choice** when starting the game to make each "
        "player pick just one category (the buttons act like radio buttons)."
    ),
    'compliment': (
        "💛 **Spin the Compliment**\n"
        "Random pairings — everyone gives one person a compliment.\n\n"
        "1. Click **Join** to enter the pool (**Leave** takes you back out)\n"
        "2. The host clicks **Close & Generate** when the pool is ready\n"
        "3. Pairings are revealed publicly — each player sees who they're giving to\n"
        "4. **Reply to** your partner, or **@mention** them, with your compliment — "
        "that's how the bot sees it was delivered\n"
        "5. Halfway through the ten-minute wrap-up the pairings are reposted with a ✅ "
        "for everyone who's delivered, and the stragglers get one nudge; then the "
        "wrap-up card says how many landed and pays the pool. **🔁 Spin Again** "
        "(host or mod) opens the next round\n\n"
        "💡 Need at least 2 players to generate pairings."
    ),
    'mfk': (
        "💍 **Marry, Fornicate, Kiss**\n"
        "Join the pool, get assigned three names, slot them into the categories.\n\n"
        "1. Click **Join** to enter the pool\n"
        "2. The host clicks **Close & Assign** when ready (need 4+ players)\n"
        "3. Each player is given 3 random names from the pool — never themselves\n"
        "4. Reply in the channel saying who you'd Marry, Fornicate, and Kiss\n\n"
        "💡 **Custom categories:** the host can pass `options:` to use any 3 categories — "
        "e.g. `Cruise, Wedding, Vacation`."
    ),
    'wyr': (
        "🤔 **Would You Rather**\n"
        "Two options per round — pick your side.\n\n"
        "1. Vote **🅰️** or **🅱️** — you can switch before the round ends\n"
        "2. The host clicks **⏭️ Next** to advance — or the round advances itself "
        "when a round timer is set (the host can still press Next early)\n"
        "3. Use **✍️ Pose Question** to queue your own (format: `option A | option B`)\n"
        "4. Votes are anonymous — press **👀 Show My Vote** to put your name beside your pick\n"
        "5. After the last round (10 by default), or when the host presses **🏁 End Game**, "
        "the recap shows the most divisive question and pays everyone who voted\n\n"
        "💡 Questions come from the bank by default — your queued questions get used first. "
        "If the bank has nothing, the round waits for someone to pose a question. "
        "On a scheduled game, anyone who voted can press Next once the round has been open a while."
    ),
    'nhie': (
        "⛔ **Never Have I Ever**\n"
        "A statement is read each round — confess or claim innocence.\n\n"
        "1. Vote **😈 Guilty** if you've done it, **😇 Innocent** if you haven't\n"
        "2. Use **✍️ Pose Statement** to queue your own statement\n"
        "3. The host clicks **⏭️ Next** to advance — or the round advances itself "
        "when a round timer is set (the host can still press Next early)\n"
        "4. After the last round (10 by default), or when the host presses **🏁 End Game**, "
        "the final guilt board is posted and everyone who played is paid\n\n"
        "❤️ **Lives mode (default 3):** every guilty vote costs you a heart. "
        "Last one standing wins — once at least two people have played and someone "
        "has been knocked out. Set `lives:0` to disable elimination.\n"
        "💡 If the bank has nothing, the round waits for someone to pose a statement. "
        "On a scheduled game, anyone who voted can press Next once the round has been open a while."
    ),
    'mlt': (
        "👑 **Most Likely To**\n"
        "Vote on which player fits each prompt best.\n\n"
        "1. Click **Join** to enter the pool (need 3+ players)\n"
        "2. Each round shows a prompt — vote for the player who fits it best\n"
        "3. The most-voted player gets the crown for that round\n"
        "4. Use **✍️ Pose Prompt** to queue your own\n"
        "5. The host clicks **⏭️ Next** to advance — or the round advances itself "
        "when a round timer is set (the host can still press Next early)\n"
        "6. After the last round (10 by default), or when the host presses **🏁 End Game**, "
        "the final crown standings are posted and everyone who voted is paid\n\n"
        "💡 You can vote for anyone in the pool, including yourself — but a tie at the top "
        "is settled by everyone else's votes, and a tie of nothing but self-votes crowns no one. "
        "If the bank has nothing, the round waits for someone to pose a prompt. "
        "On a scheduled game, anyone who voted can press Next once the round has been open a while."
    ),
    'ttl': (
        "🤥 **Two Truths and a Lie**\n"
        "Submit three statements — two true, one a lie. The room guesses which.\n\n"
        "1. Click **Submit Statements** and fill in your three statements + which is the lie\n"
        "2. The host clicks **Start Guessing** when everyone's submitted (need 3+ players)\n"
        "3. For each player, the room votes which statement they think is the lie\n"
        "4. Voters who get it right earn points; players who fool the room earn points too\n\n"
        "💡 Statements get shuffled before display so position doesn't give it away."
    ),
    'hottakes': (
        "🔥 **Hot Takes**\n"
        "Submit your spiciest opinion anonymously, then rate the room's takes.\n\n"
        "1. Click **Submit Hot Take** — your name is never shown (mods can still see who sent it)\n"
        "2. The host clicks **Start Voting** once at least 2 takes are in — "
        "with one, everyone would know whose it is\n"
        "3. Each take is shown one at a time in random order\n"
        "4. Vote your temperature: 🧊 Strongly Disagree → 👎 → 😐 → 👍 → 🔥 Strongly Agree — "
        "you can't rate your own take\n"
        "5. A take closes when its timer runs out (45 seconds unless the server changed it), "
        "the moment everyone has voted, or when the host presses **Next Take**\n"
        "6. The average temperature for each take is revealed at the end\n\n"
        "💡 Add `start_in:` to show a countdown on the lobby, or `take_seconds:` to set the "
        "timer (0 = the host presses Next). "
        "The host can press **Cancel Game** before voting starts to scrap the lobby."
    ),
    'story': (
        "📖 **Story Builder**\n"
        "Take turns writing one sentence to build a collaborative story.\n\n"
        "1. Click **Join** before the host starts the story\n"
        "2. On your turn, click **✍️ Write Your Sentence** — you have 2 minutes\n"
        "3. The story ends after the chosen sentence count (default 10, max 30)\n\n"
        "👁️ **Visibility modes:**\n"
        "• **Blind** — you only see the previous sentence (chaotic, default)\n"
        "• **Full** — you see the entire story so far\n\n"
        "💡 The host or a mod can **Skip** a slow writer any time; any writer can after a minute. "
        "Miss two turns in a row and you're dropped from the rotation. "
        "**Leave** takes you out mid-story."
    ),

    'ama': (
        "🎙️ **Anonymous AMA**\n"
        "Players answer anonymous questions from the room.\n\n"
        "1. Players **Volunteer** to take questions\n"
        "2. Anyone clicks **Ask a Question** to send one via popup\n"
        "3. The person asked replies — replies are signed, questions are not\n\n"
        "🎭 **Formats:**\n"
        "• **Hot Seat** — one player at a time; the seat rotates once they've "
        "answered (or passed) a few questions, after an hour, or when a mod skips it (default)\n"
        "• **Open Panel** — everyone who volunteers is listed at once; pick who "
        "to ask from a dropdown (with one other panelist the box opens straight away)\n\n"
        "🛡️ **Modes:**\n"
        "• **Unfiltered** — questions post immediately (default)\n"
        "• **Screened** — the host approves each question from their DMs before it's shown\n\n"
        "🏁 The host or a mod presses **End AMA** to close it — that posts the recap and pays "
        "everyone who asked or answered.\n"
        "💡 The bot DMs you when your anonymous question gets a reply."
    ),
    'fantasies': (
        "✨ **Fantasies & Dealbreakers**\n"
        "Anonymously share what you'd love or hate, then vote on each entry.\n\n"
        "1. The host clicks **Start Round** to open submissions\n"
        "2. Click **Submit a Fantasy** (something you'd love) or "
        "**Submit a Dealbreaker** (something you'd never tolerate), then "
        "write your entry\n"
        "3. The host closes submissions when ready\n"
        "4. Each entry is revealed one at a time — vote **Same** or **Not for me** "
        "(not on your own). An entry closes when its timer runs out (45 seconds unless "
        "the server changed it), the moment everyone has voted, or when the host presses **Next**\n"
        "5. The host can run more rounds, then presses **End Game** — that posts "
        "the recap and pays everyone who wrote or voted\n\n"
        "💡 All submissions are anonymous — only the votes are public. Mods can still see "
        "who sent an entry. Add `start_in:` to show a countdown, or `entry_seconds:` to set "
        "the timer (0 = the host presses Next)."
    ),
    'price': (
        "💰 **Name Your Price**\n"
        "A scenario is posed — something absurd, personal, or uncomfortable. "
        "Everyone secretly submits how much money it would take for them to do it. "
        "All prices are revealed sorted lowest to highest. "
        "After the reveal, the room votes on Most Reasonable and Most Unhinged.\n\n"
        "1. Press **Join** in the lobby; the host presses **Start** once two or more are in\n"
        "2. Each round, press **💵 Name Your Price** and type your amount — "
        "the round closes as soon as everyone who joined has answered\n"
        "3. With three or more prices in, the room votes; with two, the ladder is "
        "revealed and the game moves on\n\n"
        "💡 Host controls: **⏭️ Skip** closes a round early, **➕ Add Rounds** extends "
        "the game, and **End Game** posts the recap and pays everyone who played.\n"
        "💡 Scenarios come from the question bank unless the game was started with "
        "`source:` set to the host or the players — then a **📝 Write Scenario** "
        "button on the board opens the box, and the bank fills in if nobody writes one.\n"
        "💡 No cap — $0 to $999,999,999."
    ),
    'rushmore': (
        "🗿 **Mt. Rushmore Draft**\n"
        "A topic is chosen. Players draft their top 4 picks for that topic over four rounds — "
        "**blitz** (everyone picks at once, fastest fingers keep duplicates) or "
        "**snake** (one at a time; 1st picker in round 1 goes last in round 2).\n\n"
        "**No duplicates** — if someone picks it before you, it's gone.\n\n"
        "After 4 rounds, everyone's Mt. Rushmore is displayed and the room votes on the best one — "
        "anyone in the channel can vote (just not for themselves). A tie goes to whoever skipped "
        "fewer picks, then whoever drafted fastest. **🔁 Run Again** on the recap starts the next "
        "draft with whoever pressed it as host."
    ),
    'clapback': (
        "⚔️ **Clapback — How to Play**\n\n"
        "1. A funny prompt is shown to everyone\n"
        "2. Everyone writes their funniest answer (via popup)\n"
        "3. Answers are paired up head-to-head for voting\n"
        "4. The room votes on which answer is funnier\n"
        "5. Points = your vote percentage (75% of votes = 75 pts)\n"
        "6. Get ALL the votes? That's a **CLAPBACK**! (+25 bonus pts!)\n\n"
        "🎟️ **Odd number of answers?** One player sits the round out and "
        "scores that round's average — and nobody sits out twice until "
        "everyone has sat out once.\n\n"
        "⏱️ **Pacing:** a matchup closes as soon as every player who can vote "
        "has (anyone watching can vote too — that keeps it open for the full "
        "timer), and the host can **🔒 Close answers** once everyone who's "
        "writing is in.\n\n"
        "💡 **Tips:**\n"
        "• Funny beats accurate\n"
        "• Short and punchy usually wins\n"
        "• You can resubmit before time runs out\n"
        "• You can't vote on your own matchup\n"
        "• Leave mid-game and your score comes off the board"
    ),
    'legitlibs': (
        "📝 **LegitLibs — How to Play**\n\n"
        "Everyone fills in the blanks to complete a story — the results are always unhinged.\n\n"
        "**Classic mode (default):**\n"
        "1. The host starts a round and everyone joins — how many fit depends on the story\n"
        "2. The blanks go round the room one at a time — click **Submit Fills** on your turn; "
        "a **Volunteer** can rescue a blank a slow player leaves\n"
        "3. Once every blank is filled, the finished story is revealed\n\n"
        "**Quiplash mode** (`mode:quiplash`):\n"
        "1. The host starts a round and everyone joins (2 to 12)\n"
        "2. Click **Submit Fills** to open the form — fill in each blank\n"
        "3. The timer runs out → every version of the story is revealed one by one\n"
        "4. At the end, the full cast is shown so you know who wrote what\n\n"
        "**Heat tiers:** 🌶️ Flirty · 🌶️🌶️ Spicy · 🌶️🌶️🌶️ Filthy · 💀 Unhinged\n\n"
        "💡 You can resubmit to overwrite your fills before the timer runs out."
    ),
    'pressure': (
        "♨️ **Pressure Cooker**\n"
        "A high-stakes nickname duel — pump the gauge and hope it doesn't blow.\n\n"
        "1. Use `/games pressure challenge @user` to issue a challenge (optional: add custom stakes text)\n"
        "2. The target has 5 minutes to **Accept** or **Decline**\n"
        "3. Players take turns clicking **Pump** — each pump adds a random amount to the gauge\n"
        "4. First player to push the gauge past 100 **BUSTS** and loses\n"
        "5. The winner sets a nickname for the loser (default: 24 hours)\n\n"
        "⚙️ Cooldowns, sentence length, and per-channel rules are managed from the "
        "Pressure Cooker panel on the web dashboard."
    ),
}
