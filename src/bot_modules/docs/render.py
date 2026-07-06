"""Pure markdown → Discord embed-spec rendering for docs.

Deliberately has **no** ``discord`` import so it can be unit-tested in isolation
and reasoned about as a pure function: ``render_doc(title, body) -> [EmbedSpec]``.

Discord natively renders most markdown inside an embed *description*: bold,
italic, strikethrough, inline/`fenced` code, blockquotes, lists, masked links
``[text](url)`` (embeds only — not plain messages), and ``#/##/###`` headings.
So the renderer is mostly pass-through. It only imposes the structure Discord
can't express on its own:

- A line that is just ``---`` (or ``***`` / ``___``) is a **message break**:
  everything up to it becomes one message, everything after starts the next.
  Discord doesn't draw horizontal rules, so this repurposes the syntax cleanly.
- A section whose first line is a ``#``/``##``/``###`` heading promotes that
  heading to the embed **title**; the rest is the description.
- A description longer than 4096 chars is split on paragraph boundaries into
  continuation embeds — never inside a fenced code block.

Each spec becomes its own message (one embed per message). That sidesteps
Discord's 6000-char *aggregate-per-message* limit entirely: a single embed can
never exceed title(256) + description(4096) < 6000.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Discord hard limits.
EMBED_TITLE_LIMIT = 256
EMBED_DESC_LIMIT = 4096

_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,3})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")


@dataclass(frozen=True)
class EmbedSpec:
    """One rendered embed: an optional title and a description body."""

    title: str | None
    description: str


def _split_sections(body: str) -> list[str]:
    """Split the body on horizontal-rule lines (author-controlled breaks)."""
    sections: list[str] = []
    current: list[str] = []
    for line in body.split("\n"):
        if _HR_RE.match(line):
            sections.append("\n".join(current))
            current = []
        else:
            current.append(line)
    sections.append("\n".join(current))
    return sections


def _extract_heading(section: str) -> tuple[str | None, str]:
    """If the section's first non-blank line is a heading, peel it off as title."""
    lines = section.split("\n")
    idx = 0
    while idx < len(lines) and lines[idx].strip() == "":
        idx += 1
    if idx >= len(lines):
        return None, section
    m = _HEADING_RE.match(lines[idx])
    if not m:
        return None, section
    rest = "\n".join(lines[idx + 1 :])
    return m.group(2).strip(), rest


def _atomic_blocks(text: str) -> list[str]:
    """Split text into paragraph blocks, keeping fenced code blocks atomic."""
    lines = text.split("\n")
    blocks: list[str] = []
    para: list[str] = []

    def flush_para() -> None:
        if para:
            joined = "\n".join(para).strip("\n")
            if joined.strip():
                blocks.append(joined)
            para.clear()

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        fence = _FENCE_RE.match(line)
        if fence:
            flush_para()
            marker = fence.group(1)
            fence_lines = [line]
            i += 1
            while i < n:
                fence_lines.append(lines[i])
                closed = lines[i].strip().startswith(marker[0] * 3)
                i += 1
                if closed:
                    break
            blocks.append("\n".join(fence_lines))
            continue
        if line.strip() == "":
            flush_para()
            i += 1
            continue
        para.append(line)
        i += 1
    flush_para()
    return blocks


def _hard_split(text: str, limit: int) -> list[str]:
    """Last-resort split of an oversized single block, preferring line breaks."""
    pieces: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        pieces.append(remaining[:cut].rstrip("\n"))
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        pieces.append(remaining)
    return pieces


def _pack_blocks(blocks: list[str], limit: int) -> list[str]:
    """Greedily join paragraph blocks into ``<= limit`` chunks (joined by \\n\\n)."""
    chunks: list[str] = []
    current = ""
    for block in blocks:
        if len(block) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_hard_split(block, limit))
            continue
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) > limit:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def render_doc(title: str, body_md: str) -> list[EmbedSpec]:
    """Render a doc's markdown into an ordered list of embed specs.

    One spec == one message. Never returns an empty list: an empty doc yields a
    single placeholder embed so a placement always has something to show.
    """
    title = (title or "").strip()
    body = (body_md or "").replace("\r\n", "\n").replace("\r", "\n")

    specs: list[EmbedSpec] = []
    for section in _split_sections(body):
        heading, rest = _extract_heading(section)
        rest = rest.strip("\n")
        if not heading and not rest.strip():
            continue
        chunks = _pack_blocks(_atomic_blocks(rest), EMBED_DESC_LIMIT) or [""]
        for idx, chunk in enumerate(chunks):
            spec_title = heading[:EMBED_TITLE_LIMIT] if (idx == 0 and heading) else None
            specs.append(EmbedSpec(title=spec_title, description=chunk))

    # The first embed inherits the doc's title if the source didn't open with a
    # heading of its own — so every doc leads with a clear header.
    if specs and specs[0].title is None and title:
        specs[0] = EmbedSpec(title=title[:EMBED_TITLE_LIMIT], description=specs[0].description)

    if not specs:
        specs = [
            EmbedSpec(
                title=(title[:EMBED_TITLE_LIMIT] or None),
                description="*(This document is empty.)*",
            )
        ]
    return specs
