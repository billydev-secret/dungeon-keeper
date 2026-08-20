"""The guide has to be reachable, and its headings have to still be there.

Nothing here is hand-written any more: every topic addresses a section of
docs/design_guide.md and attaches the documents that own the detail. The
failure mode that buys is a renamed heading -- the section silently comes back
empty and a model designs against nothing -- so the real-corpus tests below
resolve every addressed heading against the actual guide.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dk_mcp.conventions_service import GUIDE, TOPICS, ConventionsService
from dk_mcp.paths_service import PathGuard
from dk_mcp.sections_service import extract_section
from tests.dk_mcp_fixture import make_repo, requires_real_corpus

REAL_REPO = Path(__file__).resolve().parents[1]

# Phrases the guide must keep carrying, matched case-insensitively because the
# guide is prose. Each one is a rule that makes a spec wrong if missed.
LOAD_BEARING = (
    "web dashboard",  # admin config never lives in Discord
    "frozen",  # dashboard route ids
    "no-contact",  # any member-to-member surface
    "is_nsfw",  # the age gate is Discord's, not a bot-side toggle
    "data_register.md",  # per-user tables
    "manual.html",  # the second docs surface
    "fails before the fix",  # bug-fix test discipline
    "the code wins",
)


@pytest.fixture
def service(tmp_path: Path) -> ConventionsService:
    return ConventionsService(PathGuard.for_repo(make_repo(tmp_path)))


def _topic(name: str):
    return next(t for t in TOPICS if t.name == name)


@pytest.mark.parametrize("topic", [t.name for t in TOPICS])
def test_every_topic_serves_its_guide_sections_and_its_sources(
    service: ConventionsService, topic: str
) -> None:
    body = service.get(topic)
    for heading in _topic(topic).guide_sections:
        assert heading in body
        assert "has no section" not in body
    for source in _topic(topic).sources:
        assert f"# Source: {source}" in body


@requires_real_corpus
@pytest.mark.parametrize("topic", [t.name for t in TOPICS])
def test_every_addressed_heading_resolves_in_the_real_guide(topic: str) -> None:
    """A renamed heading in the guide fails here, not silently in production."""
    guide = (REAL_REPO / GUIDE).read_text(encoding="utf-8")
    for heading in _topic(topic).guide_sections:
        found = extract_section(guide, heading)  # raises LookupError if gone
        assert found.lines > 1, f"{heading!r} is an empty section"


@requires_real_corpus
@pytest.mark.parametrize("topic", [t.name for t in TOPICS])
def test_every_topic_source_exists_in_the_real_repo(topic: str) -> None:
    guard = PathGuard.for_repo(REAL_REPO)
    for source in _topic(topic).sources:
        assert guard.resolve(source).is_file(), source


@requires_real_corpus
@pytest.mark.parametrize("rule", LOAD_BEARING)
def test_the_real_digest_carries_the_rules_that_make_a_spec_wrong(
    rule: str,
) -> None:
    digest = ConventionsService(PathGuard.for_repo(REAL_REPO)).digest().lower()
    assert rule in digest


def test_digest_is_the_guide_itself(service: ConventionsService) -> None:
    digest = service.digest()
    assert "Part 1 — The decision sequence" in digest
    assert "Part 4 — Before you commit" in digest
    assert "working-agreement" in digest  # the topic list closes it


def test_safety_topic_serves_the_no_contact_spec(
    service: ConventionsService,
) -> None:
    """The rule this guide was written for: it must be one call away."""
    body = service.get("safety")
    assert "no-contact list" in body
    assert "# Source: docs/no_contact_spec.md" in body


def test_unknown_topic_lists_the_real_ones(service: ConventionsService) -> None:
    with pytest.raises(LookupError) as exc:
        service.get("vibes")
    assert "working-agreement" in str(exc.value)


def test_missing_guide_degrades_instead_of_exploding(tmp_path: Path) -> None:
    """A renamed guide must not take the whole tool down mid-session."""
    root = make_repo(tmp_path)
    (root / GUIDE).unlink()
    body = ConventionsService(PathGuard.for_repo(root)).get("embeds")
    assert "could not be read" in body
    assert "resolve_accent_color" in body  # the source document still arrives


def test_renamed_heading_degrades_instead_of_exploding(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    path = root / GUIDE
    path.write_text(
        path.read_text(encoding="utf-8").replace("## Copy", "## Wording"),
        encoding="utf-8",
    )
    body = ConventionsService(PathGuard.for_repo(root)).get("embeds")
    assert "has no section 'Copy'" in body
    assert "resolve_accent_color" in body  # the rest of the topic survives


def test_topic_names_are_unique_and_lowercase() -> None:
    names = [t.name for t in TOPICS]
    assert len(set(names)) == len(names)
    assert all(n == n.lower() and " " not in n for n in names)
