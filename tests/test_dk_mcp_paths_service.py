"""Adversarial tests for the DK MCP server's path allowlist.

Written before the guard existed, and deliberately hostile.

The MCP server is reachable over a Cloudflare tunnel whose only protection is a
secret URL path, and the checkout it reads is *production*: `.env` holds the
Discord token, `LAVALINK_PASSWORD` and `SPOTIFY_CLIENT_SECRET`; `dungeonkeeper.db`
is the live database; `.git/` carries every secret ever committed. A path that
escapes the allowlist hands those over to anyone who learns the URL. So the
guard is an allowlist -- resolve first, then prove containment -- and these
tests are the enforcement.

The nastiest case here is `docs/token.md -> ../.env`: an allowed root, an
allowed extension, and a filename that looks like a spec. Only resolving the
symlink *before* the containment check catches it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dk_mcp.paths_service import PathDenied, PathGuard, Reason
from tests.dk_mcp_fixture import requires_real_corpus


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A miniature DK checkout carrying every hazard the real one has."""
    root = tmp_path / "dungeon-keeper"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("not ours", encoding="utf-8")

    for rel, body in {
        ".env": "DISCORD_TOKEN=hunter2\nLAVALINK_PASSWORD=swordfish\n",
        ".env.production": "DISCORD_TOKEN=hunter2\n",
        "dungeonkeeper.db": "SQLite format 3\x00",
        "dungeonkeeper.db-wal": "wal",
        "CLAUDE.md": "# Working agreement\n",
        "README.md": "# Dungeon Keeper\n",
        ".git/config": "[remote]\n",
        "lavalink/application.yml": "password: ${LAVALINK_PASSWORD}\n",
        "docs/INDEX.md": "# Documentation Index\n",
        "docs/casino_spec.md": "# Casino\n",
        "docs/plans/survivor.md": "# Survivor\n",
        "docs/reviews/2026-08-06-review-synthesis.md": "# Synthesis\n",
        "docs/testing/user_testing_checklist.md": "# Checklist\n",
        "src/bot_modules/casino/logic.py": "def spin():\n    return 7\n",
        "src/web_server/static/manual.html": "<h2 id='casino'>Casino</h2>",
        "src/web_server/static/app.js.map": "{}",
    }.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")

    # A spec-looking symlink that actually points at the token file.
    (root / "docs" / "token.md").symlink_to(root / ".env")
    # A directory symlink escaping the tree entirely.
    (root / "docs" / "escape").symlink_to(outside, target_is_directory=True)
    # A dangling symlink -- must not be servable either.
    (root / "docs" / "dangling.md").symlink_to(root / "docs" / "gone.md")
    return root


@pytest.fixture
def guard(repo: Path) -> PathGuard:
    return PathGuard.for_repo(repo)


# --------------------------------------------------------------------------
# The happy path: everything Billy scoped in, and nothing else.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "rel",
    [
        "docs/INDEX.md",
        "docs/casino_spec.md",
        "docs/plans/survivor.md",
        "CLAUDE.md",
        "src/bot_modules/casino/logic.py",
        "src/web_server/static/manual.html",
        "./docs/casino_spec.md",
        "docs/../docs/casino_spec.md",
    ],
)
def test_allows_the_served_corpus(guard: PathGuard, repo: Path, rel: str) -> None:
    resolved = guard.resolve(rel)
    assert resolved.is_file()
    assert resolved.is_relative_to(repo)


# --------------------------------------------------------------------------
# Traversal, in every dialect. Each of these is an attempt at the token.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "rel",
    [
        pytest.param("../.env", id="parent"),
        pytest.param("../../etc/passwd", id="parent-twice"),
        pytest.param("docs/../.env", id="through-allowed-root"),
        pytest.param("src/../.env", id="through-src"),
        pytest.param("docs/./../../outside/secret.md", id="dot-and-parent"),
        pytest.param("docs/../src/../.env", id="root-hopping"),
        pytest.param("docs/plans/../../.env", id="up-from-nested"),
        pytest.param("....//.env", id="dot-stuffing"),
        pytest.param("docs/escape/secret.md", id="symlinked-dir-out-of-tree"),
        pytest.param("docs/token.md", id="symlink-to-env-in-allowed-root"),
    ],
)
def test_denies_traversal(guard: PathGuard, rel: str) -> None:
    with pytest.raises(PathDenied) as exc:
        guard.resolve(rel)
    assert exc.value.reason in {Reason.OUTSIDE_ROOTS, Reason.FORBIDDEN,
                                Reason.NOT_FOUND, Reason.MALFORMED}


def test_symlink_to_env_never_yields_env_contents(guard: PathGuard, repo: Path) -> None:
    """The regression that would matter: proving we do not read through it."""
    assert (repo / "docs" / "token.md").resolve() == (repo / ".env").resolve()
    with pytest.raises(PathDenied):
        guard.resolve("docs/token.md")


@pytest.mark.parametrize(
    "rel",
    [
        pytest.param("/etc/passwd", id="absolute-outside"),
        pytest.param("/etc/shadow", id="absolute-shadow"),
        pytest.param("//etc/passwd", id="double-slash"),
        pytest.param("C:\\Windows\\win.ini", id="windows-drive"),
        pytest.param("\\\\server\\share", id="unc"),
    ],
)
def test_denies_absolute_paths(guard: PathGuard, rel: str) -> None:
    with pytest.raises(PathDenied):
        guard.resolve(rel)


def test_denies_absolute_path_to_a_file_inside_the_repo(
    guard: PathGuard, repo: Path
) -> None:
    """Even a legitimate corpus file must be addressed relatively."""
    with pytest.raises(PathDenied) as exc:
        guard.resolve(str(repo / "docs" / "casino_spec.md"))
    assert exc.value.reason is Reason.MALFORMED


def test_denies_absolute_path_to_the_real_prod_env(guard: PathGuard) -> None:
    with pytest.raises(PathDenied):
        guard.resolve("/home/ben/discord-bots/dungeon-keeper/.env")


# --------------------------------------------------------------------------
# Encoded / malformed input. We reject rather than decode: a decoder invites
# double-encoding bugs, and these are paths, not URLs.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "rel",
    [
        pytest.param("docs/%2e%2e/.env", id="urlencoded-dotdot"),
        pytest.param("..%2f.env", id="urlencoded-slash"),
        pytest.param("%2e%2e%2f%2e%2e%2f.env", id="fully-encoded"),
        pytest.param("docs%2fcasino_spec.md", id="encoded-separator"),
        pytest.param("docs\\..\\.env", id="backslash-traversal"),
        pytest.param("docs/casino_spec.md\x00.txt", id="nul-truncation"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param("~/.ssh/id_rsa", id="tilde"),
        pytest.param("~ben/.env", id="tilde-user"),
    ],
)
def test_denies_malformed_input(guard: PathGuard, rel: str) -> None:
    with pytest.raises(PathDenied):
        guard.resolve(rel)


@pytest.mark.parametrize("value", [None, 3, b"docs/INDEX.md", ["docs/INDEX.md"]])
def test_denies_non_string_input(guard: PathGuard, value: object) -> None:
    with pytest.raises(PathDenied) as exc:
        guard.resolve(value)  # type: ignore[arg-type]
    assert exc.value.reason is Reason.MALFORMED


# --------------------------------------------------------------------------
# Secrets inside an allowed root, and secrets at the repo root. Belt to the
# allowlist's braces: `.env` is already unreachable by containment, and this
# makes "never read .env" explicit rather than incidental.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "rel",
    [
        ".env",
        ".env.production",
        "dungeonkeeper.db",
        "dungeonkeeper.db-wal",
        ".git/config",
        "lavalink/application.yml",
        "README.md",
        "pyproject.toml",
    ],
)
def test_denies_repo_root_files_other_than_claude_md(
    guard: PathGuard, rel: str
) -> None:
    with pytest.raises(PathDenied):
        guard.resolve(rel)


def test_env_is_denied_even_if_planted_inside_an_allowed_root(
    guard: PathGuard, repo: Path
) -> None:
    planted = repo / "docs" / ".env"
    planted.write_text("DISCORD_TOKEN=hunter2\n", encoding="utf-8")
    with pytest.raises(PathDenied) as exc:
        guard.resolve("docs/.env")
    assert exc.value.reason in {Reason.FORBIDDEN, Reason.BAD_EXTENSION}


def test_git_dir_is_denied_even_under_an_allowed_root(
    guard: PathGuard, repo: Path
) -> None:
    nested = repo / "src" / ".git"
    nested.mkdir()
    (nested / "config.py").write_text("SECRET = 1\n", encoding="utf-8")
    with pytest.raises(PathDenied) as exc:
        guard.resolve("src/.git/config.py")
    assert exc.value.reason is Reason.FORBIDDEN


# --------------------------------------------------------------------------
# Shape of the target: regular files only, allowed extensions only.
# --------------------------------------------------------------------------

def test_denies_directories(guard: PathGuard) -> None:
    with pytest.raises(PathDenied) as exc:
        guard.resolve("docs/plans")
    assert exc.value.reason is Reason.NOT_A_FILE


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX fifos only")
def test_denies_fifos(guard: PathGuard, repo: Path) -> None:
    fifo = repo / "docs" / "pipe.md"
    os.mkfifo(fifo)
    with pytest.raises(PathDenied) as exc:
        guard.resolve("docs/pipe.md")
    assert exc.value.reason is Reason.NOT_A_FILE


def test_denies_dangling_symlink(guard: PathGuard) -> None:
    with pytest.raises(PathDenied) as exc:
        guard.resolve("docs/dangling.md")
    assert exc.value.reason is Reason.NOT_FOUND


@pytest.mark.parametrize(
    ("rel", "body"),
    [
        pytest.param("docs/notes.txt", "plain", id="txt-under-docs"),
        pytest.param("docs/data.json", "{}", id="json-under-docs"),
        pytest.param("src/secrets.pem", "-----BEGIN", id="pem-under-src"),
        pytest.param("src/id.key", "key", id="key-under-src"),
        pytest.param("src/cache.db", "sqlite", id="db-under-src"),
    ],
)
def test_denies_disallowed_extensions(
    guard: PathGuard, repo: Path, rel: str, body: str
) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    with pytest.raises(PathDenied):
        guard.resolve(rel)


def test_docs_root_serves_markdown_only(guard: PathGuard, repo: Path) -> None:
    (repo / "docs" / "helper.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(PathDenied) as exc:
        guard.resolve("docs/helper.py")
    assert exc.value.reason is Reason.BAD_EXTENSION


def test_source_maps_are_not_served(guard: PathGuard) -> None:
    with pytest.raises(PathDenied):
        guard.resolve("src/web_server/static/app.js.map")


# --------------------------------------------------------------------------
# Deliberately-excluded corpus: reviews and testing. INDEX.md points at these,
# so the caller must get "outside the corpus", never a confusing not-found.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "rel",
    [
        "docs/reviews/2026-08-06-review-synthesis.md",
        "docs/reviews/does-not-exist.md",
        "docs/testing/user_testing_checklist.md",
        "docs/../docs/reviews/2026-08-06-review-synthesis.md",
    ],
)
def test_excluded_dirs_report_outside_corpus(guard: PathGuard, rel: str) -> None:
    with pytest.raises(PathDenied) as exc:
        guard.resolve(rel)
    assert exc.value.reason is Reason.EXCLUDED
    assert "corpus" in str(exc.value).lower()


# --------------------------------------------------------------------------
# The two non-raising entry points must agree with resolve(), because search
# filters hits through them. A divergence is a leak that never raises.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "rel",
    [
        "docs/casino_spec.md",
        "docs/token.md",
        "docs/escape/secret.md",
        "docs/reviews/2026-08-06-review-synthesis.md",
        ".env",
        "src/bot_modules/casino/logic.py",
    ],
)
def test_is_allowed_agrees_with_resolve(guard: PathGuard, repo: Path, rel: str) -> None:
    candidate = repo / rel
    try:
        guard.resolve(rel)
    except PathDenied:
        assert not guard.is_allowed(candidate)
    else:
        assert guard.is_allowed(candidate)


def test_walk_never_escapes_the_allowlist(guard: PathGuard, repo: Path) -> None:
    """The invariant: nothing the indexer can enumerate is outside the roots.

    This is the test that catches a future search change which bypasses
    resolve() -- the symlinks planted in the fixture are exactly what such a
    change would start returning.
    """
    seen = list(guard.walk("docs")) + list(guard.walk("src"))
    assert seen, "walk found nothing -- fixture or guard is broken"
    for path in seen:
        assert guard.is_allowed(path)
        assert path.is_relative_to(repo)
        assert ".env" not in path.name
        assert ".git" not in path.parts
        assert "reviews" not in path.parts
        assert "testing" not in path.parts
    names = {p.name for p in seen}
    assert "token.md" not in names, "followed a symlink out of the corpus"
    assert "secret.md" not in names, "walked into a symlinked directory"


def test_walk_refuses_a_root_outside_the_allowlist(guard: PathGuard) -> None:
    for bad in ("..", "/etc", "lavalink", "docs/reviews"):
        with pytest.raises(PathDenied):
            list(guard.walk(bad))


# --------------------------------------------------------------------------
# Against the real checkout: the guard is configured for the tree it will
# actually serve, not just the fixture.
# --------------------------------------------------------------------------

REAL_REPO = Path(__file__).resolve().parents[1]


@requires_real_corpus
def test_real_repo_serves_its_corpus_and_hides_its_secrets() -> None:
    real = PathGuard.for_repo(REAL_REPO)
    assert real.resolve("docs/INDEX.md").is_file()
    assert real.resolve("CLAUDE.md").is_file()
    assert real.resolve("src/web_server/static/manual.html").is_file()
    for rel in (".env", "dungeonkeeper.db", ".git/config", "README.md",
                "requirements.txt", "lavalink/application.yml",
                "docs/reviews/2026-08-06-review-synthesis.md"):
        with pytest.raises(PathDenied):
            real.resolve(rel)


@requires_real_corpus
def test_real_repo_walk_is_all_inside_the_allowlist() -> None:
    real = PathGuard.for_repo(REAL_REPO)
    docs = list(real.walk("docs"))
    assert len(docs) > 100, "expected the full docs corpus"
    for path in docs:
        assert path.suffix == ".md"
        assert "reviews" not in path.parts
        assert "testing" not in path.parts
        assert path.is_relative_to(REAL_REPO / "docs")
