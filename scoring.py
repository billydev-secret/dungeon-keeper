"""Pure scoring math for the Dungeon Keeper member quality system.

All functions here are free of Discord API calls and database access —
they operate purely on primitives and are fully unit-testable.

The XP math functions (qualified_words, cooldown_multiplier, etc.) remain
canonical in xp_system.py and are re-exported here for a single import point.
"""

from __future__ import annotations

# Re-export XP math from xp_system so tests can import from one place
from xp_system import (  # noqa: F401
    calculate_message_xp,
    cooldown_multiplier,
    level_for_xp,
    pair_multiplier,
    qualified_words,
    role_grant_due,
    xp_required_for_level,
)


def compute_score_from_components(
    engagement: float,
    consistency: float,
    resonance: float,
    activity: float,
) -> float:
    """Compute a composite member quality score from four components.

    Weights:  engagement 40%, consistency 25%, resonance 20%, activity 15%.
    All inputs and output are expected in the range [0, 100].
    """
    return (
        0.40 * engagement
        + 0.25 * consistency
        + 0.20 * resonance
        + 0.15 * activity
    )


def clamp_score(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    """Clamp a score value to [lo, hi]."""
    return max(lo, min(hi, value))
