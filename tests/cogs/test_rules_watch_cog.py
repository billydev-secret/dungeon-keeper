"""Cog-surface tests for Rules Watch slash commands.

The labeling logic itself lives in rules_watch/service.py (covered by the
ledger/scorer suites); what's asserted here is the command surface — the
/rules-watch label verdict parameter must expose exactly the two valid
choices so mods can't type free text that silently counts as "fp".
"""
from __future__ import annotations

from discord import app_commands

from bot_modules.cogs.rules_watch_cog import RulesWatchCog


def test_rw_label_verdict_exposes_exactly_violation_and_fp():
    cmd = RulesWatchCog.rules_watch.get_command("label")
    assert isinstance(cmd, app_commands.Command)
    params = {p.name: p for p in cmd.parameters}
    choices = [(c.name, c.value) for c in params["verdict"].choices]
    assert choices == [("violation", "violation"), ("fp", "fp")]
