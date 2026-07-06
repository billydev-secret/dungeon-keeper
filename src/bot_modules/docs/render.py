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
- Headings stay inline as ``#``/``##``/``###`` markdown in the description —
  Discord renders those larger than its fixed-size embed ``title`` field, so a
  ``#`` header reads as a proper big heading. If the doc's first section has no
  heading of its own, the doc's title leads it as a ``#`` header.
- A description longer than 4096 chars is split on paragraph boundaries into
  continuation embeds — never inside a fenced code block.

Each spec becomes its own message (one embed per message). That sidesteps
Discord's 6000-char *aggregate-per-message* limit entirely: a single embed's
description can never exceed 4096 < 6000.
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
# Markdown image ``![alt](url)``. The leading ``!`` is required, so masked
# links ``[text](url)`` are left untouched (Discord renders those in-place).
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*(\S+?)\s*\)")


@dataclass(frozen=True)
class EmbedSpec:
    """One rendered embed: a description body and an optional image.

    Headings live *inside* the description as ``#``/``##``/``###`` markdown —
    Discord renders those larger than the embed ``title`` field (which is a
    fixed, roughly body-sized bold), so a ``#`` header reads as a proper big
    heading. Image markdown ``![]()`` (which Discord won't render in a
    description) is pulled out and surfaced as ``image_url`` instead.
    """

    description: str
    image_url: str | None = None


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


def _starts_with_heading(text: str) -> bool:
    """True if the first non-blank line is a ``#``/``##``/``###`` heading."""
    for line in text.split("\n"):
        if line.strip() == "":
            continue
        return _HEADING_RE.match(line) is not None
    return False


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
    lead = True  # first content section leads the whole doc
    for section in _split_sections(body):
        # Pull the first image out of the section; strip all image markdown so
        # it doesn't show as literal ``![]()`` text in the description.
        found = _IMAGE_RE.findall(section)
        image_url = found[0] if found else None
        text = _IMAGE_RE.sub("", section).strip("\n")
        if not text.strip() and not image_url:
            continue
        # Headings stay inline as markdown (Discord renders ``#`` big). If the
        # doc's first content section didn't open with its own heading, lead with
        # the doc title as a ``#`` header. Prepend before chunking so it's
        # budgeted against the 4096 limit rather than overflowing it.
        if lead and title and not _starts_with_heading(text):
            header = f"# {title[:EMBED_TITLE_LIMIT]}"
            text = f"{header}\n\n{text}" if text.strip() else header
        lead = False
        chunks = _pack_blocks(_atomic_blocks(text), EMBED_DESC_LIMIT) or [""]
        for idx, chunk in enumerate(chunks):
            # The image belongs to the section's first embed only.
            specs.append(
                EmbedSpec(description=chunk, image_url=image_url if idx == 0 else None)
            )

    if not specs:
        placeholder = "*(This document is empty.)*"
        if title:
            placeholder = f"# {title[:EMBED_TITLE_LIMIT]}\n\n{placeholder}"
        specs = [EmbedSpec(description=placeholder)]
    return specs
