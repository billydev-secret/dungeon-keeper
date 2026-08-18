"""Section addressing: the corpus is 2.7 MB, so fetches have to be targeted."""

from __future__ import annotations

from pathlib import Path

import pytest

from dk_mcp.sections_service import (
    extract_section,
    html_to_text,
    outline,
    slugify,
    strip_markdown_sections,
)
from tests.dk_mcp_fixture import make_repo

MARKDOWN = (
    "# Alpha Spec\n"
    "\n"
    "Intro.\n"
    "\n"
    "## Payouts\n"
    "\n"
    "The jackpot pays 500 coins.\n"
    "\n"
    "### Jackpot\n"
    "\n"
    "Skimmed at 5 percent.\n"
    "\n"
    "## Settings\n"
    "\n"
    "Dashboard only.\n"
)


def test_outline_nests_and_bounds_sections() -> None:
    sections = outline(MARKDOWN)
    crumbs = [s.breadcrumb for s in sections]
    assert crumbs == [
        "Alpha Spec",
        "Alpha Spec > Payouts",
        "Alpha Spec > Payouts > Jackpot",
        "Alpha Spec > Settings",
    ]
    payouts = sections[1]
    # A parent section owns its children: "the Payouts section" means all of it.
    assert payouts.start == 5
    assert payouts.end == 12  # up to the blank line before '## Settings'
    assert sections[-1].end == len(MARKDOWN.splitlines())


def test_headings_inside_code_fences_are_not_sections() -> None:
    """A '# comment' in a shell example is not a heading."""
    text = "# Doc\n\n```bash\n# not a heading\ngit commit\n```\n\n## Real\n\nBody.\n"
    assert [s.title for s in outline(text)] == ["Doc", "Real"]


@pytest.mark.parametrize(
    "wanted",
    ["Jackpot", "jackpot", "jackpot", "Alpha Spec > Payouts > Jackpot"],
)
def test_section_lookup_is_forgiving(wanted: str) -> None:
    assert extract_section(MARKDOWN, wanted).title == "Jackpot"


def test_an_exact_title_beats_a_substring_match() -> None:
    """"setup" must resolve to the section actually called Setup."""
    text = "# D\n\n## Setup\n\na\n\n## Setup notes\n\nb\n"
    assert extract_section(text, "setup").title == "Setup"


def test_ambiguous_lookup_lists_the_candidates() -> None:
    text = "# D\n\n## Release notes\n\na\n\n## Setup notes\n\nb\n"
    with pytest.raises(LookupError) as exc:
        extract_section(text, "notes")
    assert "Release notes" in str(exc.value) and "Setup notes" in str(exc.value)


def test_missing_section_names_what_exists() -> None:
    """A wrong guess should cost one round trip, not a blind retry."""
    with pytest.raises(LookupError) as exc:
        extract_section(MARKDOWN, "Refunds")
    assert "Payouts" in str(exc.value)


def test_slugify_matches_github_anchors() -> None:
    assert slugify("Limits & Payouts") == "limits-and-payouts"
    assert slugify("13. Not Yet Built") == "13-not-yet-built"


# -- manual.html ---------------------------------------------------------


@pytest.fixture
def manual(tmp_path: Path) -> str:
    root = make_repo(tmp_path)
    return (root / "src/web_server/static/manual.html").read_text(encoding="utf-8")


def test_manual_sections_keep_their_real_anchor_ids(manual: str) -> None:
    """The ids are dashboard deep links, so they are worth quoting back."""
    anchors = {s.anchor for s in outline(manual, is_html=True)}
    assert {"guide", "economy", "economy-casino", "voice"} <= anchors


def test_manual_headings_drop_their_section_numbers(manual: str) -> None:
    titles = [s.title for s in outline(manual, is_html=True)]
    assert "Economy & Perk Shop" in titles
    assert "10 Economy & Perk Shop" not in titles


def test_manual_section_extraction_returns_prose_not_markup(manual: str) -> None:
    found = extract_section(manual, "economy-casino", is_html=True)
    body = html_to_text("\n".join(manual.splitlines()[found.start - 1 : found.end]))
    assert "Every bet comes out of your wallet." in body
    assert "<p>" not in body
    assert "- Blackjack" in body


def test_html_to_text_drops_scripts_and_unescapes(manual: str) -> None:
    text = html_to_text(manual)
    assert "not prose" not in text
    assert "Economy & Perk Shop" in text
    assert "&amp;" not in text


# -- stripping whole sections -------------------------------------------


def test_strip_removes_the_section_and_its_rows() -> None:
    text = (
        "# Index\n\n## Keep\n\n| a | b |\n\n## Audits\n\n"
        "| [reviews/x.md](reviews/x.md) | gone |\n\n## Also keep\n\ntail\n"
    )
    out = strip_markdown_sections(text, ("audits",))
    assert "## Audits" not in out
    assert "reviews/x.md" not in out
    assert "## Keep" in out and "## Also keep" in out and "tail" in out


def test_strip_is_a_noop_when_the_section_is_absent() -> None:
    assert strip_markdown_sections(MARKDOWN, ("audits",)).strip() == MARKDOWN.strip()
