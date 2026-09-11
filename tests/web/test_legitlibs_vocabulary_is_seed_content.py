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
_SRC = _ROOT / "src"
_ROUTES = _SRC / "web_server" / "routes"

#: Migrations are the sanctioned editor — "edited by migration only" is the
#: decision, so 019's seed INSERTs and any later correction are not offenders.
#: Everything else under src/ is.
_MIGRATIONS = _SRC / "migrations"

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


def test_nothing_outside_a_migration_mutates_the_vocabulary_tables() -> None:
    """The route check above matches on URL wording, so it would miss an editor
    called /legitlibs/vocabulary or /legitlibs/blanks. This one reads the SQL,
    and it reads *all* of src/ rather than the route modules — because the
    layering rule sends the next author somewhere else entirely. Routes are
    glue; the only module that queries these tables today is
    `cogs/games_legitlibs/data.py`, which is exactly where a spec-compliant CRUD
    implementation would put its writes, and a route-only scan would wave it
    through."""
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if _MIGRATIONS in path.parents:
            continue
        src = path.read_text(encoding="utf-8")
        for table in VOCAB_TABLES:
            for verb in ("INSERT INTO", "INSERT OR REPLACE INTO", "INSERT OR IGNORE INTO",
                         "REPLACE INTO", "UPDATE", "DELETE FROM"):
                if re.search(rf"\b{verb}\s+{table}\b", src, re.IGNORECASE):
                    offenders.append(f"{path.relative_to(_ROOT)}: {verb} {table}")
    assert not offenders, (
        f"LegitLibs' bot-wide blank vocabulary is written outside a migration: "
        f"{offenders}. See docs/games_system_spec.md § Environment / files."
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


def test_neither_table_has_gained_a_guild_id(tmp_path) -> None:
    """Read the schema the migrations actually build, not 019's CREATE text: a
    later ALTER TABLE ... ADD COLUMN would be invisible to a parse of the
    original statement. Per-guild vocabulary is the change that would answer
    the cross-guild objection and make an editor safe — so if it lands, wire
    one up and delete this file rather than leaving the tripwire to fail."""
    import migrations
    from bot_modules.core.db_utils import open_db

    db = tmp_path / "t.db"
    migrations.apply_migrations_sync(db)
    with open_db(db) as conn:
        for table in VOCAB_TABLES:
            columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert columns, f"{table} is gone from the schema entirely"
            assert "guild_id" not in columns, (
                f"{table} has gained a guild_id: the vocabulary can now be scoped "
                "per guild, which is what this decision was waiting for"
            )
