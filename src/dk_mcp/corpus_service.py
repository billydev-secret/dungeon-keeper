"""Classification of every document the server serves.

This is the single highest-value thing the server does. ``docs/INDEX.md`` opens
with a warning that its specs are *not all equally trustworthy* -- some match
the running bot, some were written to implement a feature and have since
drifted, and some describe features that were never built at all
(``duel_minigame_flows_v2.md`` specs Liar's Dice and Minesweeper flows that do
not exist). CLAUDE.md's rule is blunt: **when a spec and the code disagree, the
code wins.**

A server that hands over an aspirational spec unlabelled will produce confident
designs for features nobody ever wrote. So no document leaves this module
without a banner naming its classification, and the tool descriptions repeat
the code-wins rule.

Where the classification comes from
-----------------------------------
``docs/INDEX.md`` is the authority, and it is machine-readable: ``## `` section
headings over markdown tables of ``| [doc](path) | summary | notes |``. The
section names the flavour; the trailing column carries the detail that matters
most in practice -- ``survey_spec.md`` is filed under *Design* but its note says
"**Zero code** ... not started", and a plan's Status column says things like
"Proposal -- nothing built" or "Not started, deliberately deferred". Those
strings ride along verbatim.

Three wrinkles the parser has to handle, all of them real:

* **A doc can be listed twice.** INDEX says outright that a few plans "double as
  the primary doc for their feature and also appear in the Design table". When
  that happens the *more cautionary* classification wins, because the whole
  point of the label is to stop the reader over-trusting the document.
* **Plans without a table row.** Many of ``docs/plans/`` is not listed in INDEX
  at all. Those get an explicit "not listed in INDEX" banner plus the plan's own
  dated header, never a silent blank -- INDEX itself says to trust a plan's own
  header over its table.
* **Dangling pointers.** INDEX's Audits section and its "start here" callout aim
  at ``docs/reviews/``, which is deliberately not served. Those rows are dropped
  from the catalogue and the fetch path refuses them with "outside the corpus"
  rather than a confusing not-found.
"""

from __future__ import annotations

import posixpath
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dk_mcp.paths_service import PathDenied, PathGuard

__all__ = ["Catalogue", "Doc", "Kind"]

INDEX_PATH = "docs/INDEX.md"
AGREEMENT_PATH = "CLAUDE.md"
MANUAL_PATH = "src/web_server/static/manual.html"


class Kind(StrEnum):
    REFERENCE = "reference"
    DESIGN = "design"
    ASPIRATIONAL = "aspirational"
    PLAN = "plan"
    AGREEMENT = "agreement"
    MANUAL = "manual"
    INDEX = "index"
    UNCLASSIFIED = "unclassified"


# When INDEX files one document under two sections, the more cautionary label
# wins: the label exists to stop a reader over-trusting the document, so the
# failure that matters is under-warning, not over-warning.
_CAUTION_ORDER: tuple[Kind, ...] = (
    Kind.ASPIRATIONAL,
    Kind.DESIGN,
    Kind.PLAN,
    Kind.REFERENCE,
)

_SECTION_KINDS: tuple[tuple[str, Kind], ...] = (
    ("reference specs", Kind.REFERENCE),
    ("design specs", Kind.DESIGN),
    ("implementation plans", Kind.PLAN),
    ("aspirational specs", Kind.ASPIRATIONAL),
)

BANNERS: dict[Kind, str] = {
    Kind.REFERENCE: (
        "REFERENCE SPEC - classified in docs/INDEX.md as matching current "
        "behavior. Still verify specifics against the code before relying on "
        "them; the code wins."
    ),
    Kind.DESIGN: (
        "DESIGN SPEC - written in order to implement the feature. The feature "
        "usually exists, but this document may lag the code in its details. "
        "Check src/ before quoting specifics. The code wins."
    ),
    Kind.ASPIRATIONAL: (
        "!! ASPIRATIONAL SPEC - docs/INDEX.md flags this as describing things "
        "that were NEVER FULLY BUILT. Commands, modules and flows named here "
        "may simply not exist. Do not design against it without checking src/ "
        "first. The code wins."
    ),
    Kind.PLAN: (
        "IMPLEMENTATION PLAN - a stage-by-stage build plan, not a description "
        "of current behavior. Read its status: a plan can be shipped, "
        "partially built, or a proposal with no code at all. The code wins."
    ),
    Kind.AGREEMENT: (
        "WORKING AGREEMENT - the binding house rules for this repo. These are "
        "requirements to follow when drafting a spec, not background reading."
    ),
    Kind.MANUAL: (
        "USER-FACING GUIDE - the manual rendered in the dashboard's own Help "
        "panel. This is NOT a dev spec: CLAUDE.md is explicit that it drifts "
        "from docs/ independently, and it is often the surface that lags. "
        "Where it disagrees with a spec, the usual resolution is that the "
        "manual is stale and must be updated in the same commit as the change."
    ),
    Kind.INDEX: (
        "CLASSIFICATION INDEX - the map that classifies every other spec as "
        "Reference / Design / Aspirational, and the authority this server uses. "
        "Its 'Audits' and 'Testing checklists' sections are omitted here: "
        "docs/reviews/ and docs/testing/ are deliberately outside this "
        "server's corpus, so those rows would be pointers to documents that "
        "cannot be fetched."
    ),
    Kind.UNCLASSIFIED: (
        "UNCLASSIFIED - this document is not listed in docs/INDEX.md, so its "
        "trustworthiness is unknown. CLAUDE.md requires an INDEX entry, so "
        "this is drift worth flagging. Treat it as unverified and check the "
        "code."
    ),
}

# INDEX sections whose rows point at docs/reviews/ and docs/testing/. Both are
# outside the served corpus, so serving these verbatim would hand the model a
# list of documents it cannot fetch -- a dangling pointer that reads as a
# missing file. Dropped from the rendered INDEX; the banner says why.
UNSERVED_INDEX_SECTIONS: tuple[str, ...] = ("audits", "testing checklists")

_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass(frozen=True)
class Doc:
    """One servable document plus everything INDEX.md says about it."""

    path: str
    kind: Kind
    title: str
    summary: str = ""
    note: str = ""
    indexed: bool = True
    also_listed_as: tuple[Kind, ...] = ()

    @property
    def banner(self) -> str:
        lines = [BANNERS[self.kind]]
        if self.kind is Kind.PLAN and self.note:
            lines.append(f"Status per INDEX.md: {self.note}")
        elif self.note:
            lines.append(f"INDEX.md notes: {self.note}")
        if self.kind is Kind.PLAN and not self.indexed:
            lines.append(
                "This plan has no row in docs/INDEX.md, so there is no "
                "recorded status for it. Read the dated header at the top of "
                "the document itself, and trust that over any assumption that "
                "it shipped."
            )
        if self.also_listed_as:
            others = ", ".join(sorted(k.value for k in self.also_listed_as))
            lines.append(
                f"docs/INDEX.md also lists this document as: {others}. The more "
                f"cautionary classification is shown above."
            )
        return "\n".join(lines)

    def render_header(self) -> str:
        head = f"# {self.path}"
        if self.summary:
            head += f"\n\n{self.summary}"
        return f"{head}\n\n---\n{self.banner}\n---\n"


@dataclass
class _Entry:
    kind: Kind
    summary: str
    note: str


class Catalogue:
    """The served corpus, classified from docs/INDEX.md.

    Rebuilt when INDEX.md changes on disk, and at most once every ``ttl``
    seconds otherwise -- the checkout is production and features merge into it
    while a chat session is open, so a process-lifetime cache would serve a
    stale map.
    """

    def __init__(self, guard: PathGuard, ttl: float = 60.0) -> None:
        self._guard = guard
        self._ttl = ttl
        self._docs: dict[str, Doc] = {}
        self._checked_at = 0.0
        self._index_stamp: tuple[float, int] | None = None

    # -- public ----------------------------------------------------------

    def docs(self) -> list[Doc]:
        self._refresh()
        return sorted(self._docs.values(), key=lambda d: (d.kind.value, d.path))

    def get(self, path: str) -> Doc:
        """Classify one document, resolving the path through the guard first."""
        real = self._guard.resolve(path)
        rel = self._guard.relpath(real)
        self._refresh()
        doc = self._docs.get(rel)
        if doc is None:
            raise PathDenied(
                f"{rel} is not part of the served document corpus",
                reason=self._not_a_doc_reason(rel),
            )
        return doc

    def by_kind(self, kind: Kind | str | None = None) -> list[Doc]:
        if kind is None:
            return self.docs()
        wanted = Kind(kind)
        return [d for d in self.docs() if d.kind is wanted]

    def paths(self) -> list[str]:
        self._refresh()
        return sorted(self._docs)

    # -- building --------------------------------------------------------

    def _refresh(self) -> None:
        now = time.monotonic()
        if self._docs and now - self._checked_at < self._ttl:
            return
        self._checked_at = now
        stamp = self._stamp()
        if self._docs and stamp == self._index_stamp:
            return
        self._index_stamp = stamp
        self._docs = self._build()

    def _stamp(self) -> tuple[float, int] | None:
        try:
            st = self._guard.resolve(INDEX_PATH).stat()
        except (PathDenied, OSError):
            return None
        return (st.st_mtime, st.st_size)

    def _build(self) -> dict[str, Doc]:
        entries = self._parse_index()
        docs: dict[str, Doc] = {}

        for real in self._guard.walk("docs"):
            rel = self._guard.relpath(real)
            docs[rel] = self._classify(rel, real, entries.get(rel, []))

        for rel, kind in (
            (AGREEMENT_PATH, Kind.AGREEMENT),
            (MANUAL_PATH, Kind.MANUAL),
            (INDEX_PATH, Kind.INDEX),
        ):
            try:
                real = self._guard.resolve(rel)
            except PathDenied:
                continue
            docs[rel] = Doc(
                path=rel,
                kind=kind,
                title=_title_of(real, fallback=rel),
                summary=_FIXED_SUMMARIES[kind],
            )
        return docs

    def _classify(self, rel: str, real: Path, listed: list[_Entry]) -> Doc:
        title = _title_of(real, fallback=rel)
        if not listed:
            is_plan = rel.startswith("docs/plans/")
            return Doc(
                path=rel,
                kind=Kind.PLAN if is_plan else Kind.UNCLASSIFIED,
                title=title,
                summary=_first_prose_line(real),
                indexed=False,
            )
        kinds = {e.kind for e in listed}
        primary = next(k for k in _CAUTION_ORDER if k in kinds)
        best = next(e for e in listed if e.kind is primary)
        notes = [e.note for e in listed if e.note]
        return Doc(
            path=rel,
            kind=primary,
            title=title,
            summary=best.summary,
            note=" | ".join(dict.fromkeys(notes)),
            also_listed_as=tuple(sorted(kinds - {primary}, key=lambda k: k.value)),
        )

    def _parse_index(self) -> dict[str, list[_Entry]]:
        """Read INDEX.md's section tables into path -> classifications.

        Only *table rows* count. Links appearing inside a note ("plan in
        [plans/foo.md](plans/foo.md)") are cross-references, not
        classifications, and treating them as such would silently mark dozens
        of unlisted plans as classified.
        """
        try:
            text = self._guard.resolve(INDEX_PATH).read_text(encoding="utf-8")
        except (PathDenied, OSError):
            return {}

        found: dict[str, list[_Entry]] = {}
        kind: Kind | None = None
        for line in text.splitlines():
            heading = _HEADING.match(line)
            if heading:
                kind = _kind_for_section(heading.group(2))
                continue
            if kind is None or not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 2 or set(cells[0]) <= {"-", ":"}:
                continue
            link = _LINK.search(cells[0])
            if link is None:
                continue
            rel = _docs_relative(link.group(1))
            if rel is None:
                continue
            note = cells[2] if len(cells) > 2 else ""
            found.setdefault(rel, []).append(
                _Entry(kind=kind, summary=cells[1], note=note)
            )
        return found

    def _not_a_doc_reason(self, rel: str):
        from dk_mcp.paths_service import Reason

        return Reason.EXCLUDED if rel.startswith("docs/") else Reason.OUTSIDE_ROOTS


_FIXED_SUMMARIES: dict[Kind, str] = {
    Kind.AGREEMENT: (
        "The repo's working agreement: design philosophy, the docs contract, "
        "test requirements, commit gates and conventions."
    ),
    Kind.MANUAL: (
        "The user-facing guide rendered in the dashboard's Help panel "
        "(a different surface from docs/, and it drifts independently)."
    ),
    Kind.INDEX: (
        "The documentation index: classifies every spec as Reference, Design "
        "or Aspirational, and states the rule that the code wins."
    ),
}


def _kind_for_section(heading: str) -> Kind | None:
    lowered = heading.strip().lower()
    for prefix, kind in _SECTION_KINDS:
        if lowered.startswith(prefix):
            return kind
    return None


def _docs_relative(target: str) -> str | None:
    """Turn an INDEX link into a repo-relative path, or None if not served.

    INDEX links are relative to ``docs/``. A few point outward (``../README.md``);
    those leave the corpus and are dropped rather than mis-filed.
    """
    target = target.split("#", 1)[0].strip()
    if not target or not target.endswith(".md"):
        return None
    rel = posixpath.normpath(posixpath.join("docs", target))
    if rel.startswith("..") or rel.startswith("/"):
        return None
    return rel


def _title_of(real: Path, *, fallback: str) -> str:
    for line in _head_lines(real, 40):
        match = _HEADING.match(line)
        if match and len(match.group(1)) == 1:
            return match.group(2).strip()
        if real.suffix == ".html" and "<h1" in line:
            stripped = re.sub(r"<[^>]+>", "", line).strip()
            if stripped:
                return stripped
    return fallback


def _first_prose_line(real: Path) -> str:
    for line in _head_lines(real, 40):
        text = line.strip()
        if text and not text.startswith("#"):
            return text[:300]
    return ""


def _head_lines(real: Path, limit: int) -> list[str]:
    try:
        with real.open(encoding="utf-8", errors="replace") as handle:
            return [line.rstrip("\n") for _, line in zip(range(limit), handle)]
    except OSError:
        return []
