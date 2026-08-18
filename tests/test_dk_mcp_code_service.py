"""Source access — the half that makes "the code wins" checkable.

Implemented without ripgrep on purpose (no rg binary on the deploy host), so
these also pin the pure-Python scan's behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dk_mcp.code_service import CodeService
from dk_mcp.paths_service import PathDenied, PathGuard
from tests.dk_mcp_fixture import make_repo, requires_real_corpus

REAL_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def service(tmp_path: Path) -> CodeService:
    return CodeService(PathGuard.for_repo(make_repo(tmp_path)))


def test_literal_search_finds_the_line(service: CodeService) -> None:
    hits = service.search("jackpot")
    assert [(h.path, h.line) for h in hits] == [
        ("src/bot_modules/casino/logic.py", 12)
    ]


def test_search_is_case_insensitive_by_default(service: CodeService) -> None:
    assert service.search("JACKPOT")
    assert not service.search("JACKPOT", case_sensitive=True)


def test_literal_mode_does_not_treat_the_pattern_as_regex(
    service: CodeService,
) -> None:
    """`payout(bet)` must search for those characters, not a group."""
    assert service.search("payout(bet)")
    assert not service.search("p.yout", regex=False)
    assert service.search("p.yout", regex=True)


def test_bad_regex_is_a_value_error_not_a_crash(service: CodeService) -> None:
    with pytest.raises(ValueError):
        service.search("(unclosed", regex=True)


def test_empty_pattern_is_rejected(service: CodeService) -> None:
    with pytest.raises(ValueError):
        service.search("   ")


def test_path_glob_filters_by_path_or_basename(service: CodeService) -> None:
    assert service.search("payout", path_glob="*logic.py")
    assert not service.search("payout", path_glob="*.js")
    assert service.files("*casino*") == [
        "src/bot_modules/casino/cog.py",
        "src/bot_modules/casino/logic.py",
    ]


def test_limit_stops_the_scan(service: CodeService) -> None:
    assert len(service.search("e", limit=2)) == 2


def test_read_returns_numbered_lines_and_a_total(service: CodeService) -> None:
    body, first, last, total = service.read(
        "src/bot_modules/casino/logic.py", start=4, end=5
    )
    assert (first, last) == (4, 5)
    assert total == 13
    assert "class Wheel:" in body
    assert body.splitlines()[0].strip().startswith("4 |")


@pytest.mark.parametrize(
    ("name", "expect"),
    [
        pytest.param("payout", "return bet * 36", id="function"),
        pytest.param("Wheel", "SLOTS = 37", id="class"),
    ],
)
def test_symbol_extracts_the_whole_block(
    service: CodeService, name: str, expect: str
) -> None:
    body, _, _ = service.symbol("src/bot_modules/casino/logic.py", name)
    assert expect in body


def test_symbol_stops_at_the_dedent(service: CodeService) -> None:
    body, start, end = service.symbol("src/bot_modules/casino/logic.py", "Wheel")
    assert start == 4
    assert "def payout" not in body


def test_missing_symbol_names_what_the_file_defines(service: CodeService) -> None:
    with pytest.raises(LookupError) as exc:
        service.symbol("src/bot_modules/casino/logic.py", "refund")
    assert "payout" in str(exc.value) and "Wheel" in str(exc.value)


# -- the boundary still holds on this path ------------------------------


@pytest.mark.parametrize(
    "path",
    ["../.env", "src/../.env", "/etc/passwd", "dungeonkeeper.db", ".env"],
)
def test_reads_outside_the_allowlist_are_refused(
    service: CodeService, path: str
) -> None:
    with pytest.raises(PathDenied):
        service.read(path)
    with pytest.raises(PathDenied):
        service.symbol(path, "anything")


def test_documents_are_not_reachable_through_the_code_tools(
    service: CodeService,
) -> None:
    """docs/ is served by the document tools, with classification banners.

    Reading a spec through get_code would strip the banner off it, which is the
    one thing this server must not allow.
    """
    with pytest.raises(PathDenied) as exc:
        service.read("docs/alpha_spec.md")
    assert "not source code" in str(exc.value)


def test_search_never_leaves_src(service: CodeService) -> None:
    for hit in service.search("e", limit=200):
        assert hit.path.startswith("src/")


@requires_real_corpus
def test_real_repo_search_finds_a_known_symbol() -> None:
    service = CodeService(PathGuard.for_repo(REAL_REPO))
    hits = service.search("def resolve_accent_color", regex=False, limit=5)
    assert hits, "the accent-color helper should be findable in src/"
    assert all(h.path.startswith("src/") for h in hits)
