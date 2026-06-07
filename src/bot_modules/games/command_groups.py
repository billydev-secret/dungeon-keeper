"""Shared app_commands Group objects for the /games command tree.

Import `games` and/or `play` in each game cog so all subcommands hang off
the same top-level group.  Exactly one cog (GamesConfigCog) calls
`bot.tree.add_command(games)` in its setup() so the tree is registered once.
"""
from discord import app_commands

games = app_commands.Group(
    name="games",
    description="Party games, duels, and game settings.",
)

play = app_commands.Group(
    name="play",
    description="Start a party game.",
    parent=games,
)
