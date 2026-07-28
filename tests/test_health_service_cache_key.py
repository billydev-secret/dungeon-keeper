"""Cache-key namespacing for bot-inclusive health payloads.

The health cache is keyed on ``(guild_id, metric_key)`` alone. Without a
namespace, one ``include_bots=1`` request would write a bot-inclusive payload
under the plain key and serve it to every default caller for the full 15-minute
TTL (and vice versa).
"""

from __future__ import annotations

import pytest

from bot_modules.services.health_service import cache_key


@pytest.mark.parametrize("metric", ["dau_mau", "gini", "cohort_retention"])
def test_default_key_is_unchanged(metric):
    """Existing cached rows keep their key, so nothing is orphaned on deploy."""
    assert cache_key(metric) == metric


@pytest.mark.parametrize("metric", ["dau_mau", "gini", "cohort_retention"])
def test_bot_inclusive_key_differs(metric):
    assert cache_key(metric, include_bots=True) != cache_key(metric)


def test_no_collision_between_variants_of_different_metrics():
    """The suffix must not make one metric's bot key equal another's plain key."""
    keys = {
        cache_key(m, include_bots=inc)
        for m in ("dau_mau", "gini", "heatmap")
        for inc in (True, False)
    }
    assert len(keys) == 6
