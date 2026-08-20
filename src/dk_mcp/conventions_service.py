"""The house rules, served from the one document that owns them.

A spec drafted without these reads plausible and lands wrong: it puts admin
config behind a slash command (CLAUDE.md forbids it), renames a dashboard route
id (frozen -- deep links, nav mappings and telemetry all key off them), adds a
per-user table with no `docs/data_register.md` row (the record of processing
activities, so an unregistered table is invisible to an erasure request), builds
a member-to-member surface that never consults the no-contact list, or forgets
that a UI change must update `manual.html` in the same commit.

Those rules live in eight different places, which is why `docs/design_guide.md`
exists: it is the entry point a person opens, holding the order the decisions
come in and the obligations that must be true before a commit lands, with a
pointer to the document that owns each detail. This service is the *machine
view of that same document* -- it is read live, never summarized here. A
hand-written second copy of the rules is precisely the drift the guide was
written to end, so there isn't one.

Each topic addresses the guide by heading and attaches the full source
documents behind it. Renaming a heading in the guide therefore breaks a topic,
which a test catches rather than silently serving an empty section.
"""

from __future__ import annotations

from dataclasses import dataclass

from dk_mcp.paths_service import PathDenied, PathGuard
from dk_mcp.sections_service import extract_section

__all__ = ["ConventionsService", "Topic", "TOPICS", "GUIDE"]

# The entry point. Everything below addresses a section of it.
GUIDE = "docs/design_guide.md"


@dataclass(frozen=True)
class Topic:
    name: str
    headline: str
    # Headings in GUIDE, in reading order.
    guide_sections: tuple[str, ...]
    # Full documents that own the detail those sections point at.
    sources: tuple[str, ...] = ()


TOPICS: tuple[Topic, ...] = (
    Topic(
        name="working-agreement",
        headline="Where features are allowed to live, and how they are shaped.",
        guide_sections=("Part 1 — The decision sequence",),
        sources=("CLAUDE.md",),
    ),
    Topic(
        name="docs-contract",
        headline="What else has to change in the same commit as the feature.",
        guide_sections=("The docs contract",),
        sources=("CLAUDE.md", "docs/INDEX.md"),
    ),
    Topic(
        name="testing",
        headline="What ships with the feature, and what the commit gate enforces.",
        guide_sections=(
            "Part 3 — What tests ship with it",
            "Enforcement",
            "Gates",
        ),
        sources=("CLAUDE.md", "docs/web_testing.md"),
    ),
    Topic(
        name="dashboard",
        headline="Dashboard IA, the frozen route ids, and the shared widgets.",
        guide_sections=(
            "2. If it's a dashboard panel — which page, and which id?",
            "Dashboard-side code",
        ),
        sources=("docs/dashboard_ia.md", "docs/web_testing.md"),
    ),
    Topic(
        name="safety",
        headline="The gates: NSFW, opt-in, and the no-contact list.",
        guide_sections=("4. Who does it put in contact, and who can see it?",),
        sources=("docs/no_contact_spec.md",),
    ),
    Topic(
        name="privacy",
        headline="Per-user data: the register, the purge decision, the notice.",
        guide_sections=("5. What data does it store?",),
        sources=("docs/data_register.md", "docs/privacy_spec.md"),
    ),
    Topic(
        name="embeds",
        headline="Embed, panel and user-facing copy style.",
        guide_sections=("Bot-side code", "Copy"),
        sources=("docs/embed_style_guide.md",),
    ),
    Topic(
        name="commits",
        headline="Commit shape, and the Testing: section that becomes a QA card.",
        guide_sections=("The commit itself",),
        sources=("CLAUDE.md",),
    ),
)

_BY_NAME = {topic.name: topic for topic in TOPICS}


class ConventionsService:
    """Serves the design guide, whole or by topic, with its sources attached."""

    def __init__(self, guard: PathGuard) -> None:
        self._guard = guard

    def names(self) -> list[str]:
        return [topic.name for topic in TOPICS]

    def digest(self) -> str:
        """The whole design guide, without the source documents behind it.

        Deliberately the entire guide rather than a teaser: it is a few
        thousand tokens, and a model that has read all of it will not design a
        slash command for admin config.
        """
        parts = [
            "DUNGEON KEEPER HOUSE RULES",
            "",
            "This is docs/design_guide.md, the repo's entry point for its "
            "design decisions and coding standards. These are requirements, "
            "not background: a spec that ignores them will be rejected at "
            "review. Call get_conventions(topic=...) for one section plus the "
            "full source documents behind it.",
            "",
            "IMPORTANT: docs/INDEX.md classifies every spec as Reference, "
            "Design or Aspirational, and where a spec and the code disagree, "
            "THE CODE WINS. Check src/ before relying on any spec's specifics.",
            "",
            "---",
            "",
            self._read(GUIDE),
            "",
            f"Topics: {', '.join(self.names())}",
        ]
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
            f"## From {GUIDE}",
            "",
            "(The guide holds the rule and points at the sources below, which "
            "own the detail.)",
            "",
        ]
        guide = self._try_read(GUIDE)
        if guide is None:
            # Say the file is gone once, rather than reporting every heading
            # as missing -- a renamed guide and a renamed heading are very
            # different problems and the note has to name the right one.
            parts.extend(["", self._unreadable(GUIDE)])
        else:
            for heading in topic.guide_sections:
                parts.extend(["", self._section(guide, heading)])
        for path in topic.sources:
            parts.extend(["", "---", "", f"# Source: {path}", ""])
            parts.append(self._read(path))
        return "\n".join(parts)

    def _section(self, guide: str, heading: str) -> str:
        """One heading's region of the guide, or an honest note if it moved."""
        try:
            found = extract_section(guide, heading)
        except LookupError as exc:
            return (
                f"[{GUIDE} has no section {heading!r}: {exc} The rule still "
                f"applies; the heading was probably renamed, which is itself "
                f"worth flagging.]"
            )
        return "\n".join(guide.splitlines()[found.start - 1 : found.end])

    def _try_read(self, path: str) -> str | None:
        try:
            return self._guard.resolve(path).read_text(
                encoding="utf-8", errors="replace"
            )
        except (PathDenied, OSError):
            return None

    def _read(self, path: str) -> str:
        body = self._try_read(path)
        return self._unreadable(path) if body is None else body

    @staticmethod
    def _unreadable(path: str) -> str:
        return (
            f"[{path} could not be read. The rules still apply; the document "
            f"may have been renamed, which is itself worth flagging.]"
        )
