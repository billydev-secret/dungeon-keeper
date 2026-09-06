"""The CI watcher's one piece of judgement: when is a conclusion worth a DM?

Everything else in scripts/ci_watch.py is a `gh` call or a Discord POST. This
is the part that decides whether you get paged, so it is the part that gets a
test — the alarm that cries every hour is the alarm you mute.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _ci_watch():
    spec = importlib.util.spec_from_file_location("ci_watch", ROOT / "scripts" / "ci_watch.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    ("previous", "conclusion", "expected"),
    [
        # First sighting: bad news is the whole point, good news is just Tuesday.
        pytest.param(None, "failure", "broke", id="first-sighting-red-pages"),
        pytest.param(None, "success", None, id="first-sighting-green-is-silent"),
        # Transitions in both directions are the only other thing worth saying.
        pytest.param("success", "failure", "broke", id="went-red"),
        pytest.param("failure", "success", "fixed", id="recovered"),
        # A main that stays red pages once, not once per run. This is the case
        # that would have mattered: 25 days red is ~600 hourly DMs otherwise.
        pytest.param("failure", "failure", None, id="still-red-stays-quiet"),
        pytest.param("success", "success", None, id="still-green-stays-quiet"),
        # A cancelled or timed-out run is not success, so it is treated as red —
        # deliberately: something stopped the suite from proving main is good.
        pytest.param("success", "cancelled", "broke", id="cancelled-counts-as-red"),
        pytest.param("cancelled", "failure", "broke", id="red-to-a-different-red-repages"),
        pytest.param("failure", "timed_out", "broke", id="red-to-timeout-repages"),
    ],
)
def test_decide(previous, conclusion, expected):
    assert _ci_watch().decide(previous, conclusion) == expected


def test_broke_message_names_the_failing_jobs():
    """The DM has to be actionable from a phone, so it says what broke."""
    mod = _ci_watch()
    run = {"conclusion": "failure", "headSha": "abcdef1234", "url": "https://example/run/1",
           "displayTitle": "Merge branch something"}
    msg = mod.compose("broke", run, ["test", "browser"])
    assert "test, browser" in msg
    assert "abcdef12" in msg
    assert "https://example/run/1" in msg


def test_fixed_message_does_not_claim_a_failure():
    mod = _ci_watch()
    run = {"conclusion": "success", "headSha": "abcdef1234", "url": "https://example/run/2",
           "displayTitle": "Merge branch fix"}
    msg = mod.compose("fixed", run, [])
    assert "green again" in msg
    assert "🔴" not in msg
