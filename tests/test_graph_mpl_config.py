"""matplotlib's config dir must land inside the repo, not under $HOME.

The unit runs ProtectHome=read-only, so the default ~/.config/matplotlib is
unwritable: matplotlib warns and rebuilds its font cache into a fresh temp dir
on every boot. Both graph modules redirect MPLCONFIGDIR before importing
matplotlib; whichever is imported first wins, so both have to set it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "module",
    [
        "bot_modules.services.activity_graphs",
        "bot_modules.services.interaction_graph",
    ],
)
def test_importing_a_graph_module_redirects_mplconfigdir(module):
    __import__(module)

    raw = os.environ.get("MPLCONFIGDIR", "")
    assert raw, "MPLCONFIGDIR was not set by importing a graph module"

    configured = Path(raw).resolve()
    assert configured == (REPO_ROOT / ".cache" / "matplotlib").resolve()


def test_mplconfigdir_is_not_under_home():
    """The whole point: $HOME is read-only in production."""
    configured = Path(os.environ["MPLCONFIGDIR"]).resolve()
    home = Path.home().resolve()

    assert not configured.is_relative_to(home / ".config"), (
        f"MPLCONFIGDIR resolved under the read-only home config dir: {configured}"
    )


def test_configured_dir_is_writable():
    """matplotlib creates the dir itself, but the parent must be writable."""
    configured = Path(os.environ["MPLCONFIGDIR"])
    configured.mkdir(parents=True, exist_ok=True)

    probe = configured / ".write-probe"
    probe.write_text("ok")
    assert probe.read_text() == "ok"
    probe.unlink()
