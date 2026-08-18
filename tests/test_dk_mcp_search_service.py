"""Search has to surface the trustworthy document first without hiding the rest.

The failure this server exists to prevent is a model designing against a spec
for a feature nobody built. Ranking is the soft half of that defence (the
reference spec outranks the aspirational one) and the banner on every hit is
the hard half.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dk_mcp.corpus_service import Catalogue, Kind
from dk_mcp.paths_service import PathGuard
from dk_mcp.search_service import SearchService
from tests.dk_mcp_fixture import make_repo, requires_real_corpus

REAL_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def service(tmp_path: Path) -> SearchService:
    guard = PathGuard.for_repo(make_repo(tmp_path))
    return SearchService(guard, Catalogue(guard))


def test_finds_the_spec_that_matches_current_behavior(
    service: SearchService,
) -> None:
    hits = service.search("jackpot")
    assert hits
    assert hits[0].path == "docs/alpha_spec.md"


def test_aspirational_ranks_below_reference_but_is_still_returned(
    service: SearchService,
) -> None:
    """Demoted, never hidden: it is still the record of what was intended."""
    hits = service.search("jackpot")
    kinds = [h.kind for h in hits]
    assert Kind.ASPIRATIONAL in kinds
    assert kinds.index(Kind.REFERENCE) < kinds.index(Kind.ASPIRATIONAL)


def test_every_hit_carries_its_classification(service: SearchService) -> None:
    for hit in service.search("jackpot"):
        assert hit.banner.strip()
        if hit.kind is Kind.ASPIRATIONAL:
            assert "NEVER FULLY BUILT" in hit.banner


def test_hits_name_the_section_to_fetch_next(service: SearchService) -> None:
    hits = service.search("skimmed")
    assert hits[0].breadcrumb == "Alpha Spec > Payouts > Jackpot"
    assert hits[0].line == 11
    assert "5 percent" in hits[0].snippet


def test_nested_sections_do_not_triple_report_one_line(
    service: SearchService,
) -> None:
    """A match in "Payouts > Jackpot" also scores for "Payouts" and the title.

    Only the deepest breadcrumb is useful, and the shallower duplicates crowd
    out other documents.
    """
    hits = [h for h in service.search("skimmed") if h.path == "docs/alpha_spec.md"]
    assert len({h.line for h in hits}) == len(hits)


def test_kind_filter_restricts_results(service: SearchService) -> None:
    hits = service.search("jackpot", kind="aspirational")
    assert hits
    assert {h.kind for h in hits} == {Kind.ASPIRATIONAL}


def test_unknown_kind_is_a_value_error(service: SearchService) -> None:
    with pytest.raises(ValueError):
        service.search("jackpot", kind="nonsense")


def test_quoted_phrases_outrank_loose_words(service: SearchService) -> None:
    loose = service.search("jackpot coins")
    exact = service.search('"pays 500 coins"')
    assert exact[0].path == "docs/alpha_spec.md"
    assert exact[0].score > loose[0].score


def test_empty_query_returns_nothing_rather_than_everything(
    service: SearchService,
) -> None:
    assert service.search("") == []
    assert service.search("   ") == []


def test_limit_is_respected(service: SearchService) -> None:
    assert len(service.search("the", limit=2)) <= 2


def test_the_manual_is_searchable_as_prose(service: SearchService) -> None:
    """manual.html is HTML; a hit must read as copy, not markup."""
    hits = [h for h in service.search("wallet") if h.kind is Kind.MANUAL]
    assert hits
    assert "<" not in hits[0].snippet
    assert "user-facing" in hits[0].banner.lower()


def test_excluded_corpora_are_unsearchable(service: SearchService) -> None:
    for hit in service.search("superseded click"):
        assert "/reviews/" not in hit.path
        assert "/testing/" not in hit.path


@requires_real_corpus
def test_real_corpus_search_finds_a_known_convention() -> None:
    guard = PathGuard.for_repo(REAL_REPO)
    service = SearchService(guard, Catalogue(guard))
    hits = service.search('"resolve_accent_color"', limit=10)
    assert hits, "the embed accent convention should be findable"
    assert any(h.path == "CLAUDE.md" for h in hits)
