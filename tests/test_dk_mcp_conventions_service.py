"""The house rules have to be complete, and their sources have to still exist.

The summaries here are hand-written, which means they can drift from the repo.
The defence is that every topic names its source documents and those are
fetched live -- so a renamed convention doc fails a test rather than silently
serving a rule with no backing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dk_mcp.conventions_service import TOPICS, ConventionsService
from dk_mcp.paths_service import PathGuard
from tests.dk_mcp_fixture import make_repo, requires_real_corpus

REAL_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def service(tmp_path: Path) -> ConventionsService:
    return ConventionsService(PathGuard.for_repo(make_repo(tmp_path)))


@pytest.mark.parametrize("topic", [t.name for t in TOPICS])
def test_every_topic_returns_rules_and_its_sources(
    service: ConventionsService, topic: str
) -> None:
    body = service.get(topic)
    assert "The non-negotiables" in body
    assert body.count("- ") >= 3
    for source in next(t for t in TOPICS if t.name == topic).sources:
        assert f"# Source: {source}" in body


@requires_real_corpus
@pytest.mark.parametrize("topic", [t.name for t in TOPICS])
def test_every_topic_source_exists_in_the_real_repo(topic: str) -> None:
    """Guards the hand-written summaries against a renamed source document."""
    guard = PathGuard.for_repo(REAL_REPO)
    for source in next(t for t in TOPICS if t.name == topic).sources:
        assert guard.resolve(source).is_file(), source


@pytest.mark.parametrize(
    "rule",
    [
        "WEB DASHBOARD",  # admin config never lives in Discord
        "FROZEN",  # dashboard route ids
        "data_register.md",  # per-user tables
        "manual.html",  # the second docs surface
        "HARD FAILURE",  # a new logic-layer file with no mapped test
        "FAILS BEFORE THE FIX",  # bug-fix test discipline
        "THE CODE WINS",
    ],
)
def test_digest_carries_the_rules_that_make_a_spec_wrong(
    service: ConventionsService, rule: str
) -> None:
    assert rule in service.digest()


def test_unknown_topic_lists_the_real_ones(service: ConventionsService) -> None:
    with pytest.raises(LookupError) as exc:
        service.get("vibes")
    assert "working-agreement" in str(exc.value)


def test_missing_source_degrades_instead_of_exploding(tmp_path: Path) -> None:
    """A renamed doc must not take the whole tool down mid-session."""
    root = make_repo(tmp_path)
    (root / "docs" / "embed_style_guide.md").unlink()
    body = ConventionsService(PathGuard.for_repo(root)).get("embeds")
    assert "resolve_accent_color" in body  # the rule survives
    assert "could not be read" in body


def test_topic_names_are_unique_and_lowercase() -> None:
    names = [t.name for t in TOPICS]
    assert len(set(names)) == len(names)
    assert all(n == n.lower() and " " not in n for n in names)
