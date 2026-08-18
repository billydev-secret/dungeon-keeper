"""The house rules, assembled instead of scattered.

A spec drafted without these reads plausible and lands wrong: it puts admin
config behind a slash command (CLAUDE.md forbids it), renames a dashboard route
id (frozen -- deep links, nav mappings and telemetry all key off them), adds a
per-user table with no `docs/data_register.md` row (the record of processing
activities, so an unregistered table is invisible to an erasure request), or
forgets that a UI change must update `manual.html` in the same commit.

Those rules live in six different places: CLAUDE.md, `docs/embed_style_guide.md`
(398 lines), `docs/dashboard_ia.md`, `docs/data_register.md`,
`docs/web_testing.md` and `docs/INDEX.md`. Making a model hunt for them means it
usually won't. So each topic here pairs a hand-written list of the
non-negotiables -- the things that make a spec wrong if missed -- with the full
source document behind them, fetched live so the detail cannot drift out of
sync with the repo.

The summaries are a reading aid and say so. The sources are authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dk_mcp.paths_service import PathDenied, PathGuard

__all__ = ["ConventionsService", "Topic", "TOPICS"]


@dataclass(frozen=True)
class Topic:
    name: str
    headline: str
    must: tuple[str, ...]
    sources: tuple[str, ...] = field(default_factory=tuple)


TOPICS: tuple[Topic, ...] = (
    Topic(
        name="working-agreement",
        headline="Where features are allowed to live, and how they are shaped.",
        must=(
            "Configuration lives on the WEB DASHBOARD, not Discord. Every "
            "admin/server setting gets an admin-gated panel in "
            "src/web_server/, filed under the right nav heading. Do not "
            "design slash commands, modals or button flows for admin config.",
            "If a feature shipped command-managed, moving its knobs to the web "
            "and DELETING the commands is the expected follow-up.",
            "Discord is for member self-service and mod actions. Prefer one "
            "ephemeral panel with buttons/modals over a sprawl of subcommands.",
            "Collapse controls: one dial with a few states beats several "
            "overlapping toggles. Consistent button shapes and sizes.",
            "NSFW gates on channel.is_nsfw() -- Discord's own age gate -- never "
            "a bot-side toggle.",
            "Store minimal data. Message content is off by default, so derive "
            "metadata at ingest time. Sensitive access is opt-in.",
            "Never ship a preference or toggle that isn't enforced.",
            "If a feature genuinely seems to need in-Discord admin config, "
            "raise it and ask rather than building it.",
        ),
        sources=("CLAUDE.md",),
    ),
    Topic(
        name="docs-contract",
        headline="What else has to change in the same commit as the feature.",
        must=(
            "Behavior change => update the matching spec in docs/ AND its "
            "docs/INDEX.md classification if the flavour changed, in the SAME "
            "commit.",
            "UI/UX change (new or changed slash command, dashboard panel, "
            "embed copy, button/modal flow) => also update the user-facing "
            "docs at src/web_server/static/manual.html in the same commit. "
            "That is a different surface from docs/ and drifts independently.",
            "New table holding per-user data => a row in docs/data_register.md "
            "in the same commit, with an explicit decision: does "
            "purge_user_data clear it, or is it preserved -- and if preserved, "
            "on what Art 17(3) ground.",
            "If the column naming the member isn't one of the conventional "
            "names in privacy_service.SUBJECT_ID_COLUMNS, add it there too, or "
            "the data export cannot see the table.",
            "Member-facing data collection also needs a line in the privacy "
            "notice (manual.html, 'Your Data & Privacy').",
            "README.md is NOT on the per-commit contract. Touch it only when a "
            "whole feature area appears or disappears.",
            "Large tasks get a plan doc in docs/plans/, and commits reference "
            "their stage.",
            "docs/INDEX.md classifies every spec as Reference / Design / "
            "Aspirational. When a spec and the code disagree, THE CODE WINS.",
        ),
        sources=("CLAUDE.md", "docs/INDEX.md"),
    ),
    Topic(
        name="testing",
        headline="What ships with the feature, and what the commit gate enforces.",
        must=(
            "Every new feature and every bug fix lands with tests in the SAME "
            "commit.",
            "The unit under test is the logic/service layer: put behavior in "
            "*_logic.py / *_service.py and test it there. Cogs, views and "
            "embeds are glue, exercised through the logic layer, not "
            "re-tested against Discord mocks.",
            "Cover the happy path; EVERY guard/branch, especially safety gates "
            "(NSFW is_nsfw(), opt-in, role gates) -- a passing test is the "
            "enforcement the safety rule demands.",
            "For a bug fix, write a test that FAILS BEFORE THE FIX first, and "
            "watch it fail.",
            "A bug observed in Discord still gets its failing test at the "
            "logic/service layer: reproduce the state that broke, not the "
            "Discord surface where it showed up.",
            "Prefer a pytest.param row over a new test function when covering "
            "another value variant, and check whether a shared contract table "
            "already covers it (embed accents: "
            "tests/test_embed_accent_contract.py).",
            "Coverage target is on the PATCH, not the repo: ~80% of new "
            "logic-layer lines. Never lower fail_under in pyproject.toml.",
            "The pre-commit hook runs scripts/gate.py --scoped. A NEW "
            "logic-layer file (logic.py / store.py / service.py / *_logic.py / "
            "*_service.py) with no mapped test is a HARD FAILURE.",
        ),
        sources=("CLAUDE.md", "docs/web_testing.md"),
    ),
    Topic(
        name="dashboard",
        headline="Dashboard IA, the frozen route ids, and the shared widgets.",
        must=(
            "Route ids are the bare feature name (pen-pals, role-menus) -- no "
            "config-/games-/mod- prefix. EVERY EXISTING ID IS FROZEN: deep "
            "links, the nav help: mappings and usage telemetry all key off "
            "them. Regroup and relabel nav entries freely; NEVER rename an id.",
            "Settings live with the data they produce; docs/dashboard_ia.md "
            "records which features moved into their report/queue panels and "
            "which stay under Config.",
            "Shared widgets are safe by default: table.js escapes every cell "
            "(a column opts into markup with html: true and then owns its own "
            "escaping).",
            "Config panels mount through mountAsync, so a failed first fetch "
            "renders an error with a retry, never a permanent spinner.",
            "Guild-scoped caches in config-helpers.js must be cleared in "
            "resetMetaCaches() -- a test hard-fails if a new one isn't.",
            "New/restyled panels: prefer wrapping/scrolling flex rows over "
            "fixed-width ones; the browser suite checks phone/tablet/desktop "
            "layout and panel-load health.",
        ),
        sources=("docs/dashboard_ia.md", "docs/web_testing.md"),
    ),
    Topic(
        name="privacy",
        headline="Per-user data: the register, the purge decision, the notice.",
        must=(
            "A new table holding per-user data needs a row in "
            "docs/data_register.md in the same commit. The register is the "
            "record of processing activities (Art 30); a personal-data store "
            "that isn't in it is invisible to an access or erasure request.",
            "The row must state an explicit decision: does purge_user_data "
            "clear it, or is it preserved -- and if preserved, on what Art "
            "17(3) ground, not just the engineering reason.",
            "Non-conventional member-id columns must be added to "
            "privacy_service.SUBJECT_ID_COLUMNS or the export cannot see them.",
            "Member-facing collection needs a line in the privacy notice in "
            "manual.html ('Your Data & Privacy').",
            "Store minimal data: message content is off by default, so derive "
            "metadata at ingest time rather than retaining text.",
            "Sensitive access is opt-in; NSFW gating uses channel.is_nsfw().",
        ),
        sources=("docs/data_register.md", "docs/privacy_spec.md"),
    ),
    Topic(
        name="embeds",
        headline="Embed, panel and user-facing copy style.",
        must=(
            "New embeds take their color from resolve_accent_color(db_path, "
            "guild). Keep red/green/etc. only where the color is semantic.",
            "Section spacing, monospace tables, persistent views and ping "
            "allow-listing follow docs/embed_style_guide.md.",
            "The guide also covers card anatomy, the Title Case ruling, error "
            "and ❌ style, voice and terminology, and dashboard copy.",
        ),
        sources=("docs/embed_style_guide.md",),
    ),
    Topic(
        name="commits",
        headline="Commit shape, and the Testing: section that becomes a QA card.",
        must=(
            "Subject: 'Scope: descriptive summary', about 60 characters, e.g. "
            "'Pen Pals: dashboard question bank + AI prompt studio'.",
            "Prose body: why, edge cases handled, what tests cover it.",
            "NO Co-Authored-By or Claude-Session trailers.",
            "A behavior-changing commit ends its body with a 'Testing:' "
            "section listing what to verify on the live server, as '- [ ]' "
            "checkbox lines. A post-commit hook reads it and posts a QA "
            "Tracker card to Discord automatically.",
            "Work happens in a git worktree (/dk-feature) and merges back to "
            "main when ready for user testing (/dk-ship).",
            "This checkout is PRODUCTION. Never restart the bot or dashboard "
            "unasked -- the user pushes that button.",
        ),
        sources=("CLAUDE.md",),
    ),
)

_BY_NAME = {topic.name: topic for topic in TOPICS}


class ConventionsService:
    """Serves the assembled house rules, with their sources attached."""

    def __init__(self, guard: PathGuard) -> None:
        self._guard = guard

    def names(self) -> list[str]:
        return [topic.name for topic in TOPICS]

    def digest(self) -> str:
        """Every topic's headline and rules, without the source documents.

        Deliberately the whole rule set rather than a teaser: it is a few
        thousand tokens, and a model that has read all of it will not design a
        slash command for admin config.
        """
        parts = [
            "DUNGEON KEEPER HOUSE RULES",
            "",
            "Assembled from CLAUDE.md and the convention docs. These are "
            "requirements, not background: a spec that ignores them will be "
            "rejected at review. Call get_conventions(topic=...) for the full "
            "source document behind any section.",
            "",
            "IMPORTANT: docs/INDEX.md classifies every spec as Reference, "
            "Design or Aspirational, and where a spec and the code disagree, "
            "THE CODE WINS. Check src/ before relying on any spec's specifics.",
        ]
        for topic in TOPICS:
            parts.append("")
            parts.append(f"## {topic.name} — {topic.headline}")
            parts.extend(f"- {rule}" for rule in topic.must)
        parts.append("")
        parts.append(f"Topics: {', '.join(self.names())}")
        return "\n".join(parts)

    def get(self, name: str) -> str:
        topic = _BY_NAME.get(name.strip().lower())
        if topic is None:
            raise LookupError(
                f"No conventions topic {name!r}. Available: "
                f"{', '.join(self.names())}"
            )
        parts = [
            f"# Conventions: {topic.name}",
            "",
            topic.headline,
            "",
            "## The non-negotiables",
            "",
            "(A reading aid assembled from the sources below, which are "
            "authoritative.)",
            "",
        ]
        parts.extend(f"- {rule}" for rule in topic.must)
        for path in topic.sources:
            parts.extend(["", "---", "", f"# Source: {path}", ""])
            parts.append(self._read(path))
        return "\n".join(parts)

    def _read(self, path: str) -> str:
        try:
            return self._guard.resolve(path).read_text(
                encoding="utf-8", errors="replace"
            )
        except (PathDenied, OSError) as exc:
            return (
                f"[{path} could not be read: {exc}. The rules above still "
                f"apply; the source document may have been renamed, which is "
                f"itself worth flagging.]"
            )
