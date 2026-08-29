"""One place builds a Discord deep link, and it defaults to the message.

CLAUDE.md / ``embed_style_guide.md`` § Pointing at things: a channel-only link
lands the reader at the bottom of the channel instead of on the thing you were
telling them about. ``core/utils.jump_url`` and its sibling ``channel_url``
exist so the choice between the two is made deliberately and named in the call;
a hand-rolled f-string is how a message id quietly goes missing (the AMA answer
DM shipped that way for months).

The JS side has the same shape: ``audit-helpers.jumpLink`` takes an optional
message id and is the only builder panels should reach for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

#: The helpers themselves, and the two JS files that legitimately build a link.
_ALLOWED = {
    Path("bot_modules/core/utils.py"),
    # The shared JS builder — message id optional, exactly the same contract.
    Path("web_server/static/js/audit-helpers.js"),
    # Turns a ``<#id>`` written in help copy into an anchor. It does build a
    # URL, but the target is fixed by its source: a channel mention names a
    # channel, and there is no message id in one to point at.
    Path("web_server/static/js/panels/help.js"),
}

_NEEDLE = "discord.com/channels"


def _offenders(suffix: str) -> list[str]:
    hits = []
    for path in sorted(SRC.rglob(f"*{suffix}")):
        rel = path.relative_to(SRC)
        if rel in _ALLOWED:
            continue
        if _NEEDLE in path.read_text(encoding="utf-8"):
            hits.append(str(rel))
    return hits


@pytest.mark.parametrize("suffix", [".py", ".js"])
def test_no_hand_rolled_discord_links(suffix):
    offenders = _offenders(suffix)
    assert not offenders, (
        "Build Discord deep links with core/utils.jump_url (or channel_url "
        "when the channel really is the subject), never a literal URL: "
        + ", ".join(offenders)
    )


def test_jump_url_and_channel_url_differ_by_the_message():
    from bot_modules.core.utils import channel_url, jump_url

    assert jump_url(1, 2, 3) == "https://discord.com/channels/1/2/3"
    assert channel_url(1, 2) == "https://discord.com/channels/1/2"
    # A DM has no guild, and Discord addresses that namespace as "@me".
    assert jump_url("@me", 2, 3) == "https://discord.com/channels/@me/2/3"
