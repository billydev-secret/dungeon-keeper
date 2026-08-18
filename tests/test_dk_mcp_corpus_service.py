"""Classification is the thing this server exists to get right.

An unlabelled aspirational spec makes a model design confidently for a feature
nobody ever wrote, so these tests hold the labelling to its contract: the
flavour comes from docs/INDEX.md, the cautionary label wins a conflict, an
unlisted plan says so out loud, and the excluded corpora stay excluded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dk_mcp.corpus_service import UNSERVED_INDEX_SECTIONS, Catalogue, Kind
from dk_mcp.paths_service import PathDenied, PathGuard, Reason
from dk_mcp.sections_service import strip_markdown_sections
from tests.dk_mcp_fixture import make_repo, requires_real_corpus

REAL_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def catalogue(tmp_path: Path) -> Catalogue:
    return Catalogue(PathGuard.for_repo(make_repo(tmp_path)))


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        pytest.param("docs/alpha_spec.md", Kind.REFERENCE, id="reference"),
        pytest.param("docs/beta_spec.md", Kind.DESIGN, id="design"),
        pytest.param("docs/zeta_spec.md", Kind.ASPIRATIONAL, id="aspirational"),
        pytest.param("docs/plans/epsilon.md", Kind.PLAN, id="plan"),
        pytest.param("CLAUDE.md", Kind.AGREEMENT, id="agreement"),
        pytest.param("docs/INDEX.md", Kind.INDEX, id="index"),
        pytest.param(
            "src/web_server/static/manual.html", Kind.MANUAL, id="manual"
        ),
    ],
)
def test_kind_comes_from_the_index(
    catalogue: Catalogue, path: str, kind: Kind
) -> None:
    assert catalogue.get(path).kind is kind


def test_aspirational_banner_says_the_code_wins(catalogue: Catalogue) -> None:
    banner = catalogue.get("docs/zeta_spec.md").banner
    assert "NEVER FULLY BUILT" in banner
    assert "code wins" in banner.lower()


def test_every_kind_has_a_banner_and_every_banner_warns(
    catalogue: Catalogue,
) -> None:
    """No document may reach a caller without a classification."""
    for doc in catalogue.docs():
        assert doc.banner.strip(), doc.path
        assert doc.render_header().startswith(f"# {doc.path}")


def test_index_notes_ride_along(catalogue: Catalogue) -> None:
    """The Notes column often carries the real warning.

    docs/INDEX.md files survey_spec.md as a *design* spec and then says "Zero
    code ... not started" in its notes -- the note is the load-bearing part.
    """
    doc = catalogue.get("docs/gamma_spec.md")
    assert doc.kind is Kind.DESIGN
    assert "Zero code" in doc.note
    assert "Zero code" in doc.banner


def test_plan_status_is_surfaced(catalogue: Catalogue) -> None:
    doc = catalogue.get("docs/plans/epsilon.md")
    assert "Proposal" in doc.note
    assert "Status per INDEX.md: Proposal" in doc.banner


def test_a_doc_listed_twice_takes_the_cautionary_label(
    catalogue: Catalogue,
) -> None:
    """INDEX lists a few plans in the Design table as well.

    Design outranks Plan here because the label exists to stop the reader
    over-trusting the document, so under-warning is the failure that matters.
    """
    doc = catalogue.get("docs/plans/delta.md")
    assert doc.kind is Kind.DESIGN
    assert Kind.PLAN in doc.also_listed_as
    assert "also lists this document as: plan" in doc.banner


def test_unlisted_plan_admits_it_has_no_recorded_status(
    catalogue: Catalogue,
) -> None:
    doc = catalogue.get("docs/plans/unlisted.md")
    assert doc.kind is Kind.PLAN
    assert doc.indexed is False
    assert "no row in docs/INDEX.md" in doc.banner
    assert "dated header" in doc.banner


def test_cross_reference_links_are_not_classifications(tmp_path: Path) -> None:
    """A link inside a Notes cell is a pointer, not a filing.

    Counting them would silently mark unlisted plans as classified -- which is
    exactly what a naive "find every markdown link" parser does.
    """
    root = make_repo(tmp_path)
    index = root / "docs" / "INDEX.md"
    index.write_text(
        index.read_text(encoding="utf-8").replace(
            "| Beta, built but drifting | Built 2026-01-01 |",
            "| Beta | Built; plan in [plans/unlisted.md](plans/unlisted.md) |",
        ),
        encoding="utf-8",
    )
    doc = Catalogue(PathGuard.for_repo(root)).get("docs/plans/unlisted.md")
    assert doc.indexed is False


@pytest.mark.parametrize(
    "path",
    ["docs/reviews/2026-01-01-audit.md", "docs/testing/user.md", "README.md"],
)
def test_excluded_material_is_not_in_the_catalogue(
    catalogue: Catalogue, path: str
) -> None:
    assert path not in catalogue.paths()
    with pytest.raises(PathDenied):
        catalogue.get(path)


def test_excluded_paths_explain_themselves(catalogue: Catalogue) -> None:
    with pytest.raises(PathDenied) as exc:
        catalogue.get("docs/reviews/2026-01-01-audit.md")
    assert exc.value.reason is Reason.EXCLUDED
    assert "corpus" in str(exc.value)


def test_index_rebuilds_when_it_changes_on_disk(tmp_path: Path) -> None:
    """The checkout is production and features merge into it mid-session."""
    root = make_repo(tmp_path)
    catalogue = Catalogue(PathGuard.for_repo(root), ttl=0.0)
    assert catalogue.get("docs/beta_spec.md").kind is Kind.DESIGN

    index = root / "docs" / "INDEX.md"
    index.write_text(
        index.read_text(encoding="utf-8")
        .replace("| [beta_spec.md](beta_spec.md) | Beta, built but drifting |"
                 " Built 2026-01-01 |", "")
        .replace(
            "| [zeta_spec.md](zeta_spec.md) | Zeta flows | Never built |",
            "| [zeta_spec.md](zeta_spec.md) | Zeta flows | Never built |\n"
            "| [beta_spec.md](beta_spec.md) | Beta | Turned out unbuilt |",
        ),
        encoding="utf-8",
    )
    assert catalogue.get("docs/beta_spec.md").kind is Kind.ASPIRATIONAL


@requires_real_corpus
def test_unserved_index_sections_are_strippable() -> None:
    """The Audits section points at docs/reviews/, which is not served.

    Serving it verbatim would hand the reader a list of documents that cannot
    be fetched, which reads as a broken server rather than a scoping decision.
    """
    real = PathGuard.for_repo(REAL_REPO)
    text = real.resolve("docs/INDEX.md").read_text(encoding="utf-8")
    stripped = strip_markdown_sections(text, UNSERVED_INDEX_SECTIONS)
    assert "## Audits" in text
    assert "## Audits" not in stripped
    assert "](reviews/" not in stripped
    assert "](testing/" not in stripped
    # The classification tables themselves must survive the surgery.
    for heading in ("## Reference specs", "## Design specs", "## Aspirational"):
        assert heading in stripped


@requires_real_corpus
def test_real_corpus_is_fully_classified() -> None:
    """Every served document carries a classification, and only INDEX.md is
    allowed to be unclassified-by-nature (it does not list itself)."""
    catalogue = Catalogue(PathGuard.for_repo(REAL_REPO))
    docs = catalogue.docs()
    assert len(docs) > 100
    assert not [d for d in docs if d.kind is Kind.UNCLASSIFIED], (
        "a doc under docs/ has no INDEX.md row — CLAUDE.md requires one, so "
        "this is real drift worth reporting rather than a test to relax"
    )
    assert {d.path for d in docs} >= {
        "CLAUDE.md",
        "docs/INDEX.md",
        "src/web_server/static/manual.html",
    }
    assert not [d for d in docs if "/reviews/" in d.path or "/testing/" in d.path]
