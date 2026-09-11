"""LegitLibs' blank vocabulary is read-only from the dashboard, on purpose.

`legitlibs_blank_axes` and `legitlibs_blank_prompts` drive the template
editor's dropdowns, `validate_template`'s tier rules and every in-game fill
prompt, and nothing can write them: they are seeded by migration 019 and edited
by migration since. Finding #91 of the 2026-08-29 config audit asked for a CRUD
surface and was closed won't-do on 2026-09-11 — the reason is in
`docs/games_system_spec.md` § Environment / files, and it is not "nobody got
round to it".

Both tables are **bot-wide**. They carry no `guild_id` and sit in
`guild_purge_service.GLOBAL_TABLES` as "Bot-wide by design", so an editor on
any guild's dashboard would reword prompts and re-gate domains for every guild
the bot serves, including guilds this instance does not run. Raising an axis's
`min_tier` can additionally invalidate already-published templates, because
`validate_template` re-checks the tier rule when a round loads one.

So this file is a tripwire, not a coverage exercise. A future tidy-up pass that
adds the "obviously missing" endpoints will fail here, and the failure points at
the two questions that have to be answered first: whose vocabulary is it, and
what happens to published templates when an axis tightens. Deleting this test is
a fine way to close it — deleting it deliberately is the whole point.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_ROUTES = _ROOT / "src" / "web_server" / "routes"

VOCAB_TABLES = ("legitlibs_blank_axes", "legitlibs_blank_prompts")

#: Route paths that may mention the vocabulary at all, and the only verb they
#: may use. The editor needs the axes to populate its dropdowns.
READ_ONLY_PATHS = {"/legitlibs/axes": "get"}

_DECORATOR = re.compile(r'@router\.(get|post|put|patch|delete)\(\s*"([^"]+)"')


def _route_table() -> list[tuple[str, str, str]]:
    """Every (module, verb, path) declared across the dashboard's routers."""
    found = []
    for path in sorted(_ROUTES.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        for verb, route in _DECORATOR.findall(src):
            found.append((path.name, verb, route))
    return found


def test_no_route_writes_the_blank_vocabulary() -> None:
    """A create/update/delete endpoint for the axes or the prompts is the
    change this test exists to stop, whatever it is called."""
    offenders = [
        (module, verb, route)
        for module, verb, route in _route_table()
        if verb != "get"
        and re.search(r"axes|axis|blank[-_]?prompt", route, re.IGNORECASE)
    ]
    assert not offenders, (
        "these routes write LegitLibs' bot-wide blank vocabulary: "
        f"{offenders}. See docs/games_system_spec.md § Environment / files — the "
        "cross-guild question has to be answered before this content becomes "
        "editable from any one guild's dashboard."
    )


def test_the_axes_endpoint_is_still_the_read_only_one() -> None:
    """Anchors the test above to a surface that really exists, so it cannot pass
    by the route having been renamed out from under it."""
    routes = {(verb, route) for _, verb, route in _route_table()}
    for route, verb in READ_ONLY_PATHS.items():
        assert (verb, route) in routes, (
            f"{verb.upper()} {route} is gone; this file no longer guards anything"
        )


def test_no_dashboard_route_module_mutates_the_vocabulary_tables() -> None:
    """The route check is by path, so it would miss a writer hidden behind an
    unrelated URL. This one reads the SQL instead."""
    offenders: list[str] = []
    for path in sorted(_ROUTES.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        for table in VOCAB_TABLES:
            for verb in ("INSERT INTO", "INSERT OR REPLACE INTO", "UPDATE", "DELETE FROM"):
                if re.search(rf"{verb}\s+{table}\b", src, re.IGNORECASE):
                    offenders.append(f"{path.name}: {verb} {table}")
    assert not offenders, (
        f"the dashboard writes LegitLibs' bot-wide blank vocabulary: {offenders}"
    )


def test_the_tables_are_declared_bot_wide() -> None:
    """The whole reason for the decision. If these ever gain a guild_id, the
    cross-guild objection is answered and this file should be revisited."""
    from bot_modules.services.guild_purge_service import GLOBAL_TABLES

    for table in VOCAB_TABLES:
        assert table in GLOBAL_TABLES, (
            f"{table} is no longer bot-wide — re-read finding #91 in "
            "docs/plans/dashboard-config-ia.md before adding an editor anyway"
        )

    schema = (_ROOT / "src" / "migrations" / "019_games.sql").read_text(encoding="utf-8")
    for table in VOCAB_TABLES:
        body = schema.split(f"CREATE TABLE IF NOT EXISTS {table} (", 1)[1].split(");", 1)[0]
        assert "guild_id" not in body, (
            f"{table} has gained a guild_id: per-guild vocabulary is exactly the "
            "change that would make an editor safe, so wire one up and delete this"
        )
