"""Static lookup tables for the Games Help cog.

These dicts back the ``/games-help`` slash command — one entry per
game key in :data:`bot_modules.games.constants.GAME_ICONS`. They live
in their own module so the cog stays a thin Discord-glue shim and so
tests can assert key alignment without spinning up Discord.

The alignment is load-bearing: if a game gets added to ``GAME_ICONS``
without a matching ``GAME_COMMANDS`` and ``GAME_DESCRIPTIONS`` entry,
:func:`build_help_embed` will fall back to ``"/<key>"`` and an empty
description — silent UX rot. The test in
``tests/test_games_help_logic.py`` catches that.
"""

from __future__ import annotations

SUPPORT_INVITE_URL = "https://discord.gg/7gfbYYkH"

# Slash command name for each game.
GAME_COMMANDS: dict[str, str] = {
    "ffa": "/ffa",
    "traditional": "/traditional",
    "compliment": "/compliment",
    "mfk": "/mfk",
    "wyr": "/wyr",
    "nhie": "/nhie",
    "mlt": "/mlt",
    "ttl": "/twotruths",
    "hottakes": "/hottakes",
    "story": "/story",
    "ama": "/ama",
    "fantasies": "/fantasies",
    "price": "/price",
    "rushmore": "/rushmore",
    "clapback": "/clapback",
    "legitlibs": "/legitlibs",
}

# Short one-line descriptions for the help list.
GAME_DESCRIPTIONS: dict[str, str] = {
    "ffa": "Ask the server a question — everyone replies.",
    "traditional": "Classic truth or dare with SFW/NSFW categories.",
    "compliment": "Random pairings — give your match a compliment.",
    "mfk": "Assign three names to each player. You know the rest.",
    "wyr": "Vote between two options each round.",
    "nhie": "Guilty or innocent? Find out who has done what.",
    "mlt": "Vote on who fits each prompt the best.",
    "ttl": "Submit two truths and one lie. Fool the group.",
    "hottakes": "Submit anonymous opinions, rate them 🧊 to 🔥.",
    "story": "Take turns writing one sentence to build a story.",
    "ama": "Ask the hot seat player anything — anonymously.",
    "fantasies": "Submit anonymously, then vote Same or Not for me.",
    "price": "Name your price for absurd scenarios — vote on the most unhinged.",
    "rushmore": "Snake-draft your top 4 picks — no duplicates allowed.",
    "clapback": "Write the funniest answer head-to-head, vote for the best.",
    "legitlibs": (
        "Fill in the blanks to complete a story — everyone gets their "
        "own unhinged version."
    ),
}


OTHER_COMMANDS_VALUE: str = (
    "`/consent` — Manage your opt-in/opt-out settings\n"
    "`/consent-status` — Check your current consent status\n"
    "`/session-recap` — Recap of the current game night\n"
    "`/games` — Admin channel/game management\n"
    "`/games-support` — Join the support Discord server"
)
