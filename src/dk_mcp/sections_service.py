"""Outlining and section extraction, so the server can serve a *part* of a doc.

The corpus is 2.7 MB across 133 documents. Handing a model whole files burns
its context on material it did not ask for and buries the paragraph it needed,
so every fetch is addressable: ask for ``docs/casino_spec.md`` and get an
outline plus the head of the document; ask for a section and get that section.

Two formats, because the corpus has two:

* **Markdown** -- ATX headings (``## Foo``) define the tree. Sections nest: a
  ``##`` section includes the ``###`` sections beneath it, which is what a
  reader means when they ask for "the Payouts section".
* **manual.html** -- the user-facing guide, which is HTML rather than markdown
  and needs real extraction rather than tag-stripping. It carries clean
  ``<h2 id="economy-casino">`` anchors, and those ids are worth preserving
  verbatim: they are the fragment a dashboard deep link uses, so quoting one
  back gives the reader something they can actually navigate to.

Matching a requested section is deliberately forgiving -- slug, exact title,
or an unambiguous substring -- because a model asking for "payouts" should not
have to guess the exact casing of "Limits & Payouts".
"""

from __future__ import annotations

import html as html_mod
import re
from dataclasses import dataclass
from html.parser import HTMLParser

__all__ = [
    "Section",
    "extract_section",
    "html_to_text",
    "outline",
    "slugify",
    "strip_markdown_sections",
]

_ATX = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "section", "article", "header", "footer", "ul", "ol", "li",
        "table", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "br", "pre",
        "blockquote", "hr", "figure", "figcaption", "nav", "aside", "details",
    }
)
_DROP_TAGS = frozenset({"script", "style", "template", "svg", "noscript"})


@dataclass(frozen=True)
class Section:
    """One addressable region of a document."""

    anchor: str
    title: str
    level: int
    start: int  # 1-based, inclusive
    end: int  # 1-based, inclusive
    breadcrumb: str

    @property
    def lines(self) -> int:
        return self.end - self.start + 1


def slugify(title: str) -> str:
    """GitHub-flavoured heading slug."""
    text = re.sub(r"[^\w\s-]", "", title.lower().replace("&", "and"))
    return re.sub(r"[\s_]+", "-", text).strip("-")


def outline(text: str, *, is_html: bool = False) -> list[Section]:
    """Every heading in the document, with the line range it governs."""
    heads = _html_headings(text) if is_html else _markdown_headings(text)
    total = len(text.splitlines())
    sections: list[Section] = []
    trail: list[tuple[int, str]] = []

    for position, (line_no, level, title, anchor) in enumerate(heads):
        while trail and trail[-1][0] >= level:
            trail.pop()
        breadcrumb = " > ".join([t for _, t in trail] + [title])
        end = total
        for next_line, next_level, _, _ in heads[position + 1 :]:
            if next_level <= level:
                end = next_line - 1
                break
        sections.append(
            Section(
                anchor=anchor or slugify(title),
                title=title,
                level=level,
                start=line_no,
                end=max(end, line_no),
                breadcrumb=breadcrumb,
            )
        )
        trail.append((level, title))
    return sections


def extract_section(text: str, wanted: str, *, is_html: bool = False) -> Section:
    """Find the section a caller asked for, or say which ones exist.

    Raises:
        LookupError: with the available headings in the message, so a wrong
            guess costs one round trip instead of a blind retry.
    """
    sections = outline(text, is_html=is_html)
    if not sections:
        raise LookupError("This document has no headings to address.")

    needle = wanted.strip().lower()
    slug = slugify(wanted)
    for match in (
        [s for s in sections if s.anchor == slug or s.anchor == needle],
        [s for s in sections if s.title.lower() == needle],
        [s for s in sections if s.breadcrumb.lower() == needle],
        [s for s in sections if needle in s.title.lower()],
        [s for s in sections if needle in s.breadcrumb.lower()],
    ):
        if len(match) == 1:
            return match[0]
        if len(match) > 1:
            names = ", ".join(sorted(s.breadcrumb for s in match))
            raise LookupError(f"{wanted!r} matches several sections: {names}")

    names = ", ".join(s.breadcrumb for s in sections[:40])
    raise LookupError(f"No section matching {wanted!r}. Available: {names}")


def strip_markdown_sections(text: str, titles: tuple[str, ...]) -> str:
    """Drop whole top-level sections by title prefix.

    Used on docs/INDEX.md: its Audits and Testing-checklists sections point at
    docs/reviews/ and docs/testing/, neither of which this server serves.
    Leaving them in would hand the reader a list of documents that cannot be
    fetched.
    """
    wanted = tuple(t.lower() for t in titles)
    sections = [
        s
        for s in outline(text)
        if s.level <= 2 and s.title.lower().startswith(wanted)
    ]
    if not sections:
        return text
    drop: set[int] = set()
    for section in sections:
        drop.update(range(section.start, section.end + 1))
    kept = [
        line
        for number, line in enumerate(text.splitlines(), start=1)
        if number not in drop
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip() + "\n"


def html_to_text(fragment: str) -> str:
    """Render an HTML fragment as readable prose.

    manual.html is the user-facing guide; a model reading it wants the copy and
    the command names, not the markup.
    """
    parser = _TextExtractor()
    parser.feed(fragment)
    parser.close()
    return parser.text()


# -- internals -----------------------------------------------------------


def _markdown_headings(text: str) -> list[tuple[int, int, str, str]]:
    """Headings outside fenced code blocks, so a ``# comment`` in a shell
    example never becomes a section."""
    found: list[tuple[int, int, str, str]] = []
    fence: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        opener = _FENCE.match(line)
        if opener:
            marker = opener.group(1)
            if fence is None:
                fence = marker
            elif line.strip().startswith(fence):
                fence = None
            continue
        if fence is not None:
            continue
        match = _ATX.match(line)
        if match:
            title = match.group(2).strip()
            found.append((number, len(match.group(1)), title, slugify(title)))
    return found


class _HeadingFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[int, int, str, str]] = []
        self._level = 0
        self._anchor = ""
        self._line = 0
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if len(tag) == 2 and tag[0] == "h" and tag[1].isdigit():
            self._flush()
            self._level = int(tag[1])
            self._anchor = dict(attrs).get("id") or ""
            self._line = self.getpos()[0]
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if len(tag) == 2 and tag[0] == "h" and tag[1].isdigit():
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._level:
            self._buffer.append(data)

    def _flush(self) -> None:
        if not self._level:
            return
        title = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
        # The manual numbers its sections ("3 Games Night"); the number is
        # chrome, and keeping it would make every lookup need the digit.
        title = re.sub(r"^\d+\s+", "", title)
        if title:
            self.found.append(
                (self._line, self._level, title, self._anchor or slugify(title))
            )
        self._level = 0
        self._buffer = []

    def close(self) -> None:
        self._flush()
        super().close()


def _html_headings(text: str) -> list[tuple[int, int, str, str]]:
    parser = _HeadingFinder()
    parser.feed(text)
    parser.close()
    return parser.found


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_TAGS:
            self._skip += 1
            return
        if tag == "li":
            self._out.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self._out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self._out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._out.append(data)

    def text(self) -> str:
        joined = html_mod.unescape("".join(self._out))
        joined = re.sub(r"[ \t]+", " ", joined)
        joined = re.sub(r" *\n *", "\n", joined)
        return re.sub(r"\n{3,}", "\n\n", joined).strip()
