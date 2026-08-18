"""Ranked search across the served documents.

171 markdown files and 2.7 MB is far past what a resource dump can carry, so
search is the front door: find the relevant spec, then fetch the section that
matters. Two decisions worth stating.

**Sections, not files, are the unit.** A hit that says "casino_spec.md, somewhere
in 900 lines" costs a round trip; a hit that says "casino_spec.md > Payouts >
Jackpot, line 412" can be fetched directly. Scoring therefore runs per section
and hits carry the heading breadcrumb.

**Ranking demotes aspirational specs; it never hides them.** An aspirational
spec is still the right answer to "what did we intend for Liar's Dice", and
suppressing it would be its own kind of lie. It ranks below the documents that
match current behaviour, and it arrives carrying its warning banner, which is
what actually protects the reader.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from dk_mcp.corpus_service import Catalogue, Doc, Kind
from dk_mcp.paths_service import PathDenied, PathGuard
from dk_mcp.sections_service import Section, html_to_text, outline

__all__ = ["Hit", "SearchService"]

# How far a document's classification moves it in the ranking. Aspirational is
# heavily demoted because acting on it is the failure this server exists to
# prevent; it stays reachable because it is still the record of intent.
KIND_WEIGHT: dict[Kind, float] = {
    Kind.REFERENCE: 1.0,
    Kind.AGREEMENT: 1.0,
    Kind.INDEX: 0.9,
    Kind.DESIGN: 0.9,
    Kind.MANUAL: 0.85,
    Kind.PLAN: 0.8,
    Kind.UNCLASSIFIED: 0.6,
    Kind.ASPIRATIONAL: 0.4,
}

HEADING_BOOST = 6.0
TITLE_BOOST = 3.0
PHRASE_BOOST = 8.0
SNIPPET_CHARS = 260

_WORD = re.compile(r"[a-z0-9_]+")
_QUOTED = re.compile(r'"([^"]+)"')


@dataclass(frozen=True)
class Hit:
    path: str
    kind: Kind
    title: str
    breadcrumb: str
    line: int
    snippet: str
    score: float
    banner: str

    def render(self) -> str:
        return (
            f"{self.path}:{self.line}  [{self.kind.value}]\n"
            f"  {self.breadcrumb}\n"
            f"  {self.snippet}"
        )


@dataclass
class _Indexed:
    doc: Doc
    sections: list[Section]
    lines: list[str]
    lowered: list[str]


class SearchService:
    """In-memory search over the document corpus.

    The whole corpus is a few megabytes, so this scans linearly rather than
    maintaining an inverted index: simpler to reason about, fast enough at this
    size, and it cannot go stale in a way that silently drops a document.
    """

    def __init__(self, guard: PathGuard, catalogue: Catalogue, ttl: float = 60.0):
        self._guard = guard
        self._catalogue = catalogue
        self._ttl = ttl
        self._index: list[_Indexed] = []
        self._built_at = 0.0

    def search(
        self,
        query: str,
        *,
        kind: str | None = None,
        limit: int = 20,
    ) -> list[Hit]:
        terms, phrases = _parse_query(query)
        if not terms and not phrases:
            return []
        wanted = Kind(kind) if kind else None

        hits: list[Hit] = []
        for entry in self._ensure_index():
            if wanted is not None and entry.doc.kind is not wanted:
                continue
            hits.extend(self._score_doc(entry, terms, phrases))
        hits.sort(key=lambda h: (-h.score, h.path, h.line))
        return hits[:limit]

    def documents(self) -> list[Doc]:
        return [entry.doc for entry in self._ensure_index()]

    def body(self, doc: Doc) -> tuple[list[str], list[Section]]:
        """Raw lines and outline for one document, from the cache when warm."""
        for entry in self._ensure_index():
            if entry.doc.path == doc.path:
                return entry.lines, entry.sections
        real = self._guard.resolve(doc.path)
        text = real.read_text(encoding="utf-8", errors="replace")
        return text.splitlines(), outline(text, is_html=doc.path.endswith(".html"))

    # -- internals -------------------------------------------------------

    def _ensure_index(self) -> list[_Indexed]:
        now = time.monotonic()
        if self._index and now - self._built_at < self._ttl:
            return self._index
        self._built_at = now
        built: list[_Indexed] = []
        for doc in self._catalogue.docs():
            try:
                text = self._guard.resolve(doc.path).read_text(
                    encoding="utf-8", errors="replace"
                )
            except (OSError, PathDenied):
                continue
            lines = text.splitlines()
            built.append(
                _Indexed(
                    doc=doc,
                    sections=outline(text, is_html=doc.path.endswith(".html")),
                    lines=lines,
                    lowered=[line.lower() for line in lines],
                )
            )
        self._index = built
        return built

    def _score_doc(
        self, entry: _Indexed, terms: list[str], phrases: list[str]
    ) -> list[Hit]:
        doc = entry.doc
        weight = KIND_WEIGHT.get(doc.kind, 0.7)
        title_low = doc.title.lower()
        title_bonus = TITLE_BOOST * sum(t in title_low for t in terms)

        regions: list[Section] = entry.sections or [
            Section(
                anchor="",
                title=doc.title,
                level=1,
                start=1,
                end=len(entry.lines),
                breadcrumb=doc.title,
            )
        ]

        hits: list[Hit] = []
        for section in regions:
            window = entry.lowered[section.start - 1 : section.end]
            if not window:
                continue
            blob = "\n".join(window)
            score = 0.0
            matched = False
            for term in terms:
                count = blob.count(term)
                if count:
                    matched = True
                    score += 1.0 + 0.35 * (count - 1)
            for phrase in phrases:
                count = blob.count(phrase)
                if count:
                    matched = True
                    score += PHRASE_BOOST + count
            if not matched:
                continue
            crumb = section.breadcrumb.lower()
            score += HEADING_BOOST * sum(t in crumb for t in terms)
            score += HEADING_BOOST * sum(p in crumb for p in phrases)
            score = (score + title_bonus) * weight

            line_no, snippet = self._best_line(entry, section, terms + phrases)
            hits.append(
                Hit(
                    path=doc.path,
                    kind=doc.kind,
                    title=doc.title,
                    breadcrumb=section.breadcrumb,
                    line=line_no,
                    snippet=snippet,
                    score=round(score, 3),
                    banner=doc.banner,
                )
            )
        # Sections nest, so a match inside "Payouts > Jackpot" also scores for
        # "Payouts" and for the document title, producing three hits on one
        # line. Keep the deepest breadcrumb: it is the most specific thing to
        # fetch next, and the shallower ones say nothing extra.
        deepest: dict[int, Hit] = {}
        for hit in hits:
            current = deepest.get(hit.line)
            if current is None or len(hit.breadcrumb) > len(current.breadcrumb):
                deepest[hit.line] = hit
        best = sorted(deepest.values(), key=lambda h: -h.score)
        # Cap per document so one long spec cannot crowd out the results.
        return best[:3]

    def _best_line(
        self, entry: _Indexed, section: Section, needles: list[str]
    ) -> tuple[int, str]:
        best_line = section.start
        best_score = -1
        for offset in range(section.start - 1, min(section.end, len(entry.lowered))):
            low = entry.lowered[offset]
            score = sum(low.count(n) for n in needles)
            if score > best_score:
                best_score, best_line = score, offset + 1
        raw = entry.lines[best_line - 1] if best_line <= len(entry.lines) else ""
        if entry.doc.path.endswith(".html"):
            raw = html_to_text(raw).replace("\n", " ")
        text = re.sub(r"\s+", " ", raw).strip()
        if len(text) > SNIPPET_CHARS:
            text = text[: SNIPPET_CHARS - 1].rstrip() + "…"
        return best_line, text or "(no text on the matching line)"


def _parse_query(query: str) -> tuple[list[str], list[str]]:
    """Split a query into bare terms and "quoted phrases"."""
    lowered = query.lower()
    phrases = [p.strip() for p in _QUOTED.findall(lowered) if p.strip()]
    remainder = _QUOTED.sub(" ", lowered)
    terms = [w for w in _WORD.findall(remainder) if len(w) > 1]
    return terms, phrases
