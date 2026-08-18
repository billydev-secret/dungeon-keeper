"""Search and read the bot's source, so "the code wins" is actionable.

CLAUDE.md's rule -- when a spec and the code disagree, the code wins -- is only
useful if the model can actually check. This module is the checking half: grep
``src/`` for a symbol, read the function, confirm whether the thing the spec
describes exists.

Implemented in pure Python rather than by shelling out to ripgrep. The deploy
host has no ``rg`` binary (the ``rg`` on an interactive shell here is a function
wrapping Claude Code's bundled copy), so a subprocess would be an install-time
dependency that fails at runtime on a machine where nobody noticed. It also
removes an argv-construction surface from a network-facing service for no real
cost: ``src/`` is ~250k lines, and a linear scan over it takes tens of
milliseconds.

Every path yielded here has already passed the allowlist in
:mod:`dk_mcp.paths_service`; ``path_glob`` only ever filters that set further,
and is never used to build a path.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path

from dk_mcp.paths_service import PathDenied, PathGuard, Reason

__all__ = ["CodeHit", "CodeService"]

CODE_ROOT = "src"
# Minified bundles and vendored assets are all one line and match everything;
# skipping them keeps results readable.
MAX_SCAN_BYTES = 2_000_000
MAX_LINE_CHARS = 400
DEFAULT_LIMIT = 40
MAX_READ_LINES = 600


@dataclass(frozen=True)
class CodeHit:
    path: str
    line: int
    text: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.text}"


class CodeService:
    def __init__(self, guard: PathGuard) -> None:
        self._guard = guard

    # -- search ----------------------------------------------------------

    def search(
        self,
        pattern: str,
        *,
        path_glob: str | None = None,
        regex: bool = False,
        case_sensitive: bool = False,
        limit: int = DEFAULT_LIMIT,
    ) -> list[CodeHit]:
        """Find ``pattern`` in the bot's source.

        Raises:
            ValueError: on an unusable regex, rather than letting `re` raise
                something the transport would render as an internal error.
        """
        if not pattern.strip():
            raise ValueError("Search pattern is empty.")
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            needle = re.compile(pattern if regex else re.escape(pattern), flags)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression: {exc}") from exc

        hits: list[CodeHit] = []
        for real in self._guard.walk(CODE_ROOT):
            rel = self._guard.relpath(real)
            if path_glob and not _glob_matches(rel, path_glob):
                continue
            try:
                if real.stat().st_size > MAX_SCAN_BYTES:
                    continue
                with real.open(encoding="utf-8", errors="replace") as handle:
                    for number, line in enumerate(handle, start=1):
                        if needle.search(line):
                            hits.append(
                                CodeHit(rel, number, _trim(line))
                            )
                            if len(hits) >= limit:
                                return hits
            except OSError:
                continue
        return hits

    def files(self, path_glob: str | None = None, limit: int = 400) -> list[str]:
        """List source files, for orienting in the module layout."""
        found: list[str] = []
        for real in self._guard.walk(CODE_ROOT):
            rel = self._guard.relpath(real)
            if path_glob and not _glob_matches(rel, path_glob):
                continue
            found.append(rel)
            if len(found) >= limit:
                break
        return found

    # -- reading ---------------------------------------------------------

    def read(
        self,
        path: str,
        *,
        start: int | None = None,
        end: int | None = None,
    ) -> tuple[str, int, int, int]:
        """Return (text, start, end, total_lines) for a source file or range."""
        real = self._require_code(path)
        lines = real.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)
        first = max(1, start or 1)
        last = min(total, end or first + MAX_READ_LINES - 1)
        if last - first + 1 > MAX_READ_LINES:
            last = first + MAX_READ_LINES - 1
        body = "\n".join(
            f"{number:>5} | {lines[number - 1]}" for number in range(first, last + 1)
        )
        return body, first, last, total

    def symbol(self, path: str, name: str) -> tuple[str, int, int]:
        """Extract one ``def``/``class`` block by name.

        Raises:
            LookupError: naming what the file does define, so a near-miss costs
                one round trip.
        """
        real = self._require_code(path)
        lines = real.read_text(encoding="utf-8", errors="replace").splitlines()
        opener = re.compile(
            rf"^(\s*)(?:async\s+def|def|class)\s+{re.escape(name)}\b"
        )
        for index, line in enumerate(lines):
            match = opener.match(line)
            if not match:
                continue
            indent = len(match.group(1))
            end = len(lines)
            for forward in range(index + 1, len(lines)):
                candidate = lines[forward]
                if not candidate.strip():
                    continue
                if len(candidate) - len(candidate.lstrip()) <= indent:
                    end = forward
                    break
            # `end` is a 0-based exclusive index, which is the same number as
            # the last 1-based line of the block. Range to end + 1, or the
            # closing line of every def is silently dropped.
            last = min(end, index + MAX_READ_LINES)
            while last > index + 1 and not lines[last - 1].strip():
                last -= 1
            body = "\n".join(
                f"{number:>5} | {lines[number - 1]}"
                for number in range(index + 1, last + 1)
            )
            return body, index + 1, last

        defined = _definitions(lines)
        hint = ", ".join(defined[:30]) or "nothing"
        raise LookupError(f"{path} defines no {name!r}. It defines: {hint}")

    # -- internals -------------------------------------------------------

    def _require_code(self, path: str) -> Path:
        real = self._guard.resolve(path)
        rel = self._guard.relpath(real)
        if not rel.startswith(f"{CODE_ROOT}/"):
            raise PathDenied(
                f"{rel} is not source code; use the document tools for that.",
                reason=Reason.OUTSIDE_ROOTS,
            )
        return real


def _glob_matches(rel: str, pattern: str) -> bool:
    """Match a glob against the whole path or its basename.

    ``*.py`` should behave the way a person means it, not the way fnmatch
    treats a path separator.
    """
    return fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(
        rel.rsplit("/", 1)[-1], pattern
    )


def _definitions(lines: list[str]) -> list[str]:
    found = []
    pattern = re.compile(r"^\s*(?:async\s+def|def|class)\s+(\w+)")
    for line in lines:
        match = pattern.match(line)
        if match:
            found.append(match.group(1))
    return found


def _trim(line: str) -> str:
    text = line.rstrip("\n")
    if len(text) > MAX_LINE_CHARS:
        return text[: MAX_LINE_CHARS - 1].rstrip() + "…"
    return text
