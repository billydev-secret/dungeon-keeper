"""/beta slash commands.

register_all(bot) wires every slash module's commands onto bot.tree.
"""

from __future__ import annotations

from beta_tools.slash.ambient import register as register_ambient
from beta_tools.slash.help import register as register_help
from beta_tools.slash.puppets import register as register_puppets


def register_all(bot) -> None:
    register_help(bot)
    register_puppets(bot)
    register_ambient(bot)
