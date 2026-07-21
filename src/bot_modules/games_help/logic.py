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

# Slash command name for each game. The party games launch under the
# ``/games play`` group; the duel/standalone games (pressure, risky_roll)
# have their own entry points.
GAME_COMMANDS: dict[str, str] = {
    "ffa": "/games play ffa",
    "ffa_banner": "/games play ffa_banner",
    "traditional": "/games play traditional",
    "compliment": "/games play compliment",
    "mfk": "/games play mfk",
    "wyr": "/games play wyr",
    "nhie": "/games play nhie",
    "mlt": "/games play mlt",
    "ttl": "/games play twotruths",
    "hottakes": "/games play hottakes",
    "story": "/games play story",
    "ama": "/games play ama",
    "fantasies": "/games play fantasies",
    "price": "/games play price",
    "rushmore": "/games play rushmore",
    "clapback": "/games play clapback",
    "legitlibs": "/games play legitlibs",
    "pressure": "/games pressure challenge",
    "risky_roll": "/risky start",
}

# Short one-line descriptions for the help list.
GAME_DESCRIPTIONS: dict[str, str] = {
    "ffa": "A Truth or Dare prompt drops — reply anonymously right in the channel.",
    "ffa_banner": "Just drops a Truth or Dare prompt card in the channel for open chat.",
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
    "pressure": "1v1 pressure duel — pump the gauge, don't be the one who pops it.",
    "risky_roll": "Roll the dice — the highest and lowest rolls face off with a question.",
}


OTHER_COMMANDS_VALUE: str = (
    "`/games help` — Browse every game mode\n"
    "`/games support` — Join the support Discord server\n"
    "`/recap` — Recap of the current game night\n"
    "`/games end` — End the game running in this channel\n"
    "`/games join` · `/games leave` — Hop into or out of a running game"
)
