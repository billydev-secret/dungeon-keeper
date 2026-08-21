"""The AMA sticky bottom bar's re-send path.

``_resend_ama_bottom`` deletes the bar and re-posts it lower, then persists
the new message id so crash recovery can rebind it. The persist is the part
that matters after a restart: recovery reads ``bottom_message_id`` and, when
it can't fetch that message, registers the view without one — which leaves
``_bottom_msg`` unset, and an unset ``_bottom_msg`` makes this very function
return early. The bar then stops following the conversation for the rest of
the AMA, with nothing anywhere saying why.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot_modules.cogs import games_ama_cog as ama_mod
from bot_modules.games_ama.logic import AMA_FORMAT_HOT_SEAT


class _FakeBottomMsg:
    def __init__(self, mid: int):
        self.id = mid
        self.deleted = False

    async def delete(self):
        self.deleted = True


def _wire(monkeypatch, *, persist_raises: Exception | None = None):
    """Minimal bot/view/channel graph for one re-send."""
    game_id = "ama-1"
    ama_view = MagicMock()
    ama_view.db = MagicMock()
    ama_view.game_format = AMA_FORMAT_HOT_SEAT
    ama_view.queue = []
    ama_view._hot_seat_name = "Robin"
    ama_view._bottom_msg = _FakeBottomMsg(100)

    bottom_view = MagicMock()
    bottom_view.message_id = 100

    bot = MagicMock()
    bot.active_views = {game_id: ama_view, f"{game_id}_bottom": bottom_view}

    channel = MagicMock()
    channel.send = AsyncMock(return_value=_FakeBottomMsg(200))

    stored: list[dict] = []

    async def _modify_payload(db, gid, fn):
        if persist_raises:
            raise persist_raises
        payload: dict = {}
        fn(payload)
        stored.append(payload)
        return payload

    monkeypatch.setattr(ama_mod, "modify_payload", _modify_payload)
    return bot, game_id, channel, ama_view, bottom_view, stored


@pytest.mark.asyncio
async def test_the_new_bar_id_is_persisted_for_recovery(monkeypatch):
    bot, game_id, channel, ama_view, bottom_view, stored = _wire(monkeypatch)

    await ama_mod._resend_ama_bottom(bot, game_id, channel)

    assert stored == [{"bottom_message_id": 200}]
    assert ama_view._bottom_msg.id == 200
    assert bottom_view.message_id == 200


@pytest.mark.asyncio
async def test_a_failed_persist_is_reported(monkeypatch, caplog):
    """It was swallowed with a bare ``except Exception: pass``.

    The write can't be retried usefully — the bar is already posted — so the
    bar staying put is the right outcome. Staying put *silently* is not: the
    stored id now points at the message this call just deleted, so after the
    next restart the sticky bar quietly stops re-sticking. The launch path
    (``_launch``) already logs a warning for the identical write; this one
    disagreeing with it was an oversight, not a decision.
    """
    bot, game_id, channel, *_ = _wire(monkeypatch, persist_raises=RuntimeError("db gone"))

    with caplog.at_level(logging.WARNING, logger="bot_modules.cogs.games_ama_cog"):
        await ama_mod._resend_ama_bottom(bot, game_id, channel)

    assert [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "a failed bottom-bar persist must not be silent"
    )


@pytest.mark.asyncio
async def test_a_failed_persist_still_leaves_the_bar_usable(monkeypatch):
    """Best effort: the message went out, so don't unwind it over the write."""
    bot, game_id, channel, ama_view, bottom_view, _ = _wire(
        monkeypatch, persist_raises=RuntimeError("db gone")
    )

    await ama_mod._resend_ama_bottom(bot, game_id, channel)

    assert ama_view._bottom_msg.id == 200  # in-memory handle is current
    assert bottom_view.message_id == 200
    assert ama_view._suppress_resend is False  # the guard was released


@pytest.mark.asyncio
async def test_resend_is_skipped_when_there_is_no_bar_to_move(monkeypatch):
    """Exactly the state a lost persist leaves behind after a restart."""
    bot, game_id, channel, ama_view, *_ = _wire(monkeypatch)
    ama_view._bottom_msg = None

    await ama_mod._resend_ama_bottom(bot, game_id, channel)

    channel.send.assert_not_awaited()
