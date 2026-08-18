"""Shared miniature repo for the dk_mcp service tests.

Deliberately not a conftest: scripts/gate.py treats any conftest.py as a
broadly-shared file and falls back to the whole suite, which would make every
commit touching these tests pause for a full run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REAL_REPO = Path(__file__).resolve().parents[1]

# The remote test runner (docs/dev_remote_testing.md) syncs a partial checkout:
# it has src/ and tests/ but not the docs corpus. Tests that assert against the
# real tree must SKIP there rather than fail -- the same rule the missing image
# assets follow. They still run locally and in CI, which is where they matter.
_HAS_REAL_CORPUS = (REAL_REPO / "docs" / "INDEX.md").is_file() and (
    REAL_REPO / "CLAUDE.md"
).is_file()

requires_real_corpus = pytest.mark.skipif(
    not _HAS_REAL_CORPUS,
    reason="checkout without the docs corpus (remote test runner)",
)

INDEX = """# Documentation Index

> Read this first: specs come in three flavors.

## Reference specs (match current behavior)

| Doc | What it covers |
|---|---|
| [README.md](../README.md) | Outside the corpus on purpose |
| [alpha_spec.md](alpha_spec.md) | Alpha, which matches the code |
| [embed_style_guide.md](embed_style_guide.md) | Embed conventions |

## Design specs (written to implement; may lag the code)

| Doc | What it covers | Notes |
|---|---|---|
| [beta_spec.md](beta_spec.md) | Beta, built but drifting | Built 2026-01-01 |
| [gamma_spec.md](gamma_spec.md) | Gamma | **Zero code** — not started. |
| [plans/delta.md](plans/delta.md) | Delta doubles as its own plan | Shipped |

## Implementation plans (`docs/plans/`)

| Plan | What it covers | Status |
|---|---|---|
| [plans/delta.md](plans/delta.md) | Delta rollout | **Complete 2026-02-02** |
| [plans/epsilon.md](plans/epsilon.md) | Epsilon | Proposal — nothing built |

## Testing checklists (`docs/testing/`)

| Doc | What it covers |
|---|---|
| [testing/user.md](testing/user.md) | Not served |

## Aspirational specs (not fully built)

| Doc | What it covers | Why it's aspirational |
|---|---|---|
| [zeta_spec.md](zeta_spec.md) | Zeta flows | Never built |

## Audits

| Doc | What it covers |
|---|---|
| [reviews/2026-01-01-audit.md](reviews/2026-01-01-audit.md) | Not served |
"""

FILES: dict[str, str] = {
    "docs/INDEX.md": INDEX,
    "docs/alpha_spec.md": (
        "# Alpha Spec\n\nAlpha handles wagers.\n\n"
        "## Payouts\n\nThe jackpot pays 500 coins.\n\n"
        "### Jackpot\n\nSkimmed at 5 percent.\n\n"
        "## Settings\n\nConfigured on the dashboard.\n"
    ),
    "docs/beta_spec.md": "# Beta Spec\n\nBeta drifted.\n\n## Behavior\n\nBeta pings.\n",
    "docs/gamma_spec.md": "# Gamma Spec\n\nGamma is unbuilt.\n",
    "docs/zeta_spec.md": "# Zeta Spec\n\nZeta describes a jackpot nobody wrote.\n",
    "docs/embed_style_guide.md": "# Embed style guide\n\nUse resolve_accent_color.\n",
    "docs/dashboard_ia.md": "# Dashboard IA\n\nRoute ids are frozen.\n",
    "docs/data_register.md": "# Data register\n\nEvery per-user table gets a row.\n",
    "docs/web_testing.md": "# Web testing\n\nAuthz sweep.\n",
    "docs/privacy_spec.md": "# Privacy\n\nDeletion.\n",
    "docs/plans/delta.md": "# Delta plan\n\n## Stage 1\n\nBuild it.\n",
    "docs/plans/epsilon.md": "# Epsilon plan\n\nProposal only.\n",
    "docs/plans/unlisted.md": (
        "# Unlisted plan\n\nStatus: never added to INDEX.\n\n## Stage 1\n\nWork.\n"
    ),
    "docs/reviews/2026-01-01-audit.md": "# Audit\n\nSuperseded.\n",
    "docs/testing/user.md": "# Checklist\n\nClick things.\n",
    "CLAUDE.md": "# Working agreement\n\nConfig lives on the dashboard.\n",
    "README.md": "# Readme\n\nNot served.\n",
    ".env": "DISCORD_TOKEN=hunter2\n",
    "dungeonkeeper.db": "SQLite format 3\x00",
    "src/bot_modules/casino/logic.py": (
        "import math\n\n\n"
        "class Wheel:\n"
        "    SLOTS = 37\n\n"
        "    def spin(self):\n"
        "        return math.floor(1)\n\n\n"
        "def payout(bet):\n"
        "    # jackpot handling\n"
        "    return bet * 36\n"
    ),
    "src/bot_modules/casino/cog.py": "from .logic import payout\n\nCOMMAND = 'bet'\n",
    "src/web_server/static/manual.html": (
        "<html><head><style>b{}</style></head><body>\n"
        "<h1 id='guide'>Dungeon Keeper</h1>\n"
        "<h2 id='economy'>10 Economy &amp; Perk Shop</h2>\n"
        "<p>Earn coins by chatting.</p>\n"
        "<h3 id='economy-casino'>Casino</h3>\n"
        "<p>Every bet comes out of your wallet.</p>\n"
        "<ul><li>Blackjack</li><li>Roulette</li></ul>\n"
        "<script>var x = 'not prose';</script>\n"
        "<h2 id='voice'>11 Voice Control</h2>\n"
        "<p>Claim a channel.</p>\n"
        "</body></html>\n"
    ),
}


def make_repo(tmp_path: Path) -> Path:
    """Build the miniature checkout and return its root."""
    root = tmp_path / "dungeon-keeper"
    for rel, body in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root
