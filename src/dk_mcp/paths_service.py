"""The path allowlist every read in the MCP server passes through.

Why this module is paranoid
---------------------------
The server is reachable over a Cloudflare tunnel whose only protection is a
long random URL path, and the tree it reads is *production*: ``.env`` holds the
Discord token, ``LAVALINK_PASSWORD`` and ``SPOTIFY_CLIENT_SECRET``;
``dungeonkeeper.db`` is the live database full of per-user data; ``.git/``
carries every secret ever committed; ``lavalink/`` holds interpolated
credentials. One traversal bug here hands over the bot's token.

So this is an **allowlist**, never a denylist. A path is served only if, after
symlinks and ``..`` have been resolved, it lands inside an explicitly permitted
root with a permitted extension. The denylist further down is a second layer on
top of that, not the control -- ``.env`` is already unreachable by containment,
and the explicit rule exists so "never read .env" is stated rather than
incidental.

Two rules that are easy to get backwards:

* **Resolve, then check.** ``Path.resolve()`` collapses ``..`` *and* follows
  symlinks; comparing before that step is the classic hole. A file named
  ``docs/token.md`` symlinked to ``../.env`` sits in an allowed root with an
  allowed extension and is caught only after resolution.
* **Every read, including search hits.** ``walk()`` filters each candidate
  through the same check, so a symlink planted inside ``docs/`` can neither be
  fetched nor surface in a search result.

Corpus scope is a product decision, not a security one, but it lives here so
there is one answer: ``docs/reviews/`` and ``docs/testing/`` are deliberately
out of scope (superseded findings and completed QA work). ``docs/INDEX.md``
links to both, so they get their own refusal reason and a message saying they
are outside the corpus -- a dangling pointer that reads as a missing file is a
bug report waiting to happen.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

__all__ = ["PathDenied", "PathGuard", "Reason"]


class Reason(StrEnum):
    """Why a path was refused. Callers map these onto user-facing messages."""

    MALFORMED = "malformed"
    NOT_FOUND = "not-found"
    OUTSIDE_ROOTS = "outside-roots"
    EXCLUDED = "excluded"
    FORBIDDEN = "forbidden"
    NOT_A_FILE = "not-a-file"
    BAD_EXTENSION = "bad-extension"


class PathDenied(Exception):
    """A requested path is not part of the served corpus."""

    def __init__(self, message: str, *, reason: Reason) -> None:
        super().__init__(message)
        self.reason = reason


# Extensions each root may serve. docs/ is prose; src/ needs the web assets
# too, since manual.html and the dashboard panels live under it.
DOC_EXTS = frozenset({".md"})
CODE_EXTS = frozenset({".py", ".js", ".css", ".html", ".sql", ".json", ".txt", ".md"})


@dataclass(frozen=True)
class _Root:
    name: str
    exts: frozenset[str]
    # Top-level subdirectories deliberately outside the served corpus.
    excluded: frozenset[str] = frozenset()


ROOTS: tuple[_Root, ...] = (
    _Root("docs", DOC_EXTS, frozenset({"reviews", "testing"})),
    _Root("src", CODE_EXTS),
)

# Single files at the repo root that are served even though the root itself is
# not a permitted directory. CLAUDE.md is the working agreement -- the one
# convention document that does not live under docs/.
ROOT_FILES: tuple[str, ...] = ("CLAUDE.md",)

# Second layer, on top of containment. None of these is the control.
DENY_COMPONENTS = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".idea"}
)
DENY_NAME_PREFIXES = (".env",)
DENY_SUFFIXES = (".db", ".db-wal", ".db-shm", ".key", ".pem", ".pyc", ".sqlite")

# Rejected outright rather than decoded. These are filesystem paths, not URLs;
# a decoder here just invites double-encoding bugs.
_MALFORMED_SUBSTRINGS = ("%", "\\", "\x00")


@dataclass(frozen=True)
class PathGuard:
    """Validates repo-relative paths against the served corpus."""

    root: Path

    @classmethod
    def for_repo(cls, root: str | os.PathLike[str]) -> PathGuard:
        return cls(root=Path(root).resolve())

    # -- public ----------------------------------------------------------

    def resolve(self, rel: str) -> Path:
        """Turn a repo-relative path into a real one, or refuse it.

        Raises:
            PathDenied: always, rather than returning a sentinel -- a caller
                that forgets to check a boolean should not get a readable path.
        """
        text = self._require_clean_string(rel)
        self._reject_excluded_lexically(text)
        try:
            real = (self.root / text).resolve(strict=True)
        except OSError:
            raise PathDenied(
                f"No such document: {text!r}", reason=Reason.NOT_FOUND
            ) from None
        return self._check(real)

    def is_allowed(self, path: str | os.PathLike[str]) -> bool:
        """Non-raising form, for filtering candidates during a walk."""
        try:
            real = Path(path).resolve(strict=True)
        except OSError:
            return False
        try:
            self._check(real)
        except PathDenied:
            return False
        return True

    def relpath(self, path: Path) -> str:
        """The repo-relative form handed back to callers, always posix-style."""
        return path.resolve().relative_to(self.root).as_posix()

    def walk(self, rel_dir: str) -> Iterator[Path]:
        """Yield every servable file under an allowed directory.

        Symlinks are never followed and every candidate is re-validated, so a
        planted symlink can neither be walked into nor emitted.
        """
        text = self._require_clean_string(rel_dir)
        self._reject_excluded_lexically(text)
        try:
            start = (self.root / text).resolve(strict=True)
        except OSError:
            raise PathDenied(
                f"No such directory: {text!r}", reason=Reason.NOT_FOUND
            ) from None
        root = self._root_for(start)
        if not start.is_dir():
            raise PathDenied(f"Not a directory: {text!r}", reason=Reason.NOT_A_FILE)
        self._reject_excluded(start, root)

        for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
            here = Path(dirpath)
            dirnames[:] = sorted(
                d
                for d in dirnames
                if d not in DENY_COMPONENTS
                and not (here == start and d in root.excluded)
                and not (here / d).is_symlink()
            )
            for name in sorted(filenames):
                candidate = here / name
                if self.is_allowed(candidate):
                    yield candidate.resolve()

    # -- internals -------------------------------------------------------

    def _require_clean_string(self, rel: object) -> str:
        if not isinstance(rel, str):
            raise PathDenied(
                f"Path must be a string, got {type(rel).__name__}",
                reason=Reason.MALFORMED,
            )
        if not rel.strip():
            raise PathDenied("Path is empty", reason=Reason.MALFORMED)
        for bad in _MALFORMED_SUBSTRINGS:
            if bad in rel:
                raise PathDenied(
                    "Paths must be plain repo-relative paths: no URL encoding, "
                    "backslashes or null bytes",
                    reason=Reason.MALFORMED,
                )
        if rel.startswith("~"):
            raise PathDenied(
                "Home-relative paths are not served", reason=Reason.MALFORMED
            )
        if Path(rel).is_absolute() or (len(rel) > 1 and rel[1] == ":"):
            raise PathDenied(
                "Paths must be relative to the repository root",
                reason=Reason.MALFORMED,
            )
        return rel.strip()

    def _reject_excluded_lexically(self, text: str) -> None:
        """Catch excluded subdirs before resolution, so a path that does not
        exist still gets 'outside the corpus' instead of a puzzling not-found.

        Purely additive: this only ever refuses, so it cannot open a hole. The
        post-resolution check in ``_check`` is what actually enforces it.
        """
        parts = Path(os.path.normpath(text)).parts
        if len(parts) < 2:
            return
        for root in ROOTS:
            if parts[0] == root.name and parts[1] in root.excluded:
                raise self._excluded_error(root.name, parts[1])

    def _root_for(self, real: Path) -> _Root:
        for root in ROOTS:
            if real.is_relative_to(self.root / root.name):
                return root
        raise PathDenied(
            f"{self._describe(real)} is outside the served corpus "
            f"(docs/, src/, CLAUDE.md)",
            reason=Reason.OUTSIDE_ROOTS,
        )

    def _check(self, real: Path) -> Path:
        """Validate an already-resolved absolute path."""
        if real in {self.root / name for name in ROOT_FILES}:
            self._require_regular(real)
            return real

        root = self._root_for(real)
        self._reject_excluded(real, root)

        parts = real.relative_to(self.root).parts
        if any(part in DENY_COMPONENTS for part in parts):
            raise PathDenied(
                f"{self._describe(real)} is not served", reason=Reason.FORBIDDEN
            )
        if real.name.startswith(DENY_NAME_PREFIXES) or real.name.endswith(
            DENY_SUFFIXES
        ):
            raise PathDenied(
                f"{self._describe(real)} is not served", reason=Reason.FORBIDDEN
            )

        self._require_regular(real)
        if real.suffix.lower() not in root.exts:
            raise PathDenied(
                f"{root.name}/ serves "
                f"{', '.join(sorted(root.exts))} files, not {real.suffix or 'that'}",
                reason=Reason.BAD_EXTENSION,
            )
        return real

    def _reject_excluded(self, real: Path, root: _Root) -> None:
        rel = real.relative_to(self.root / root.name).parts
        if rel and rel[0] in root.excluded:
            raise self._excluded_error(root.name, rel[0])

    @staticmethod
    def _excluded_error(root_name: str, subdir: str) -> PathDenied:
        why = {
            "reviews": "superseded findings would dilute the current specs",
            "testing": "completed QA work",
        }.get(subdir, "out of scope")
        return PathDenied(
            f"{root_name}/{subdir}/ is deliberately outside this server's "
            f"corpus ({why}). INDEX.md links to it, but it is not served here.",
            reason=Reason.EXCLUDED,
        )

    @staticmethod
    def _require_regular(real: Path) -> None:
        if not stat.S_ISREG(os.stat(real).st_mode):
            raise PathDenied(
                f"{real.name} is not a regular file", reason=Reason.NOT_A_FILE
            )

    def _describe(self, real: Path) -> str:
        """A refusal message must never echo a path outside the repo back."""
        try:
            return real.relative_to(self.root).as_posix()
        except ValueError:
            return "That path"
