"""A pending challenge survives a restart with its buttons working
(duels-party-126).

The Accept / Decline view times out with the challenge, so it was never
re-attached on cog_load: a card posted in the minutes before a restart
answered every press with "interaction failed" until the sweep expired it.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
import pytest_asyncio

from bot_modules.cogs.quickdraw import db as qdb
from bot_modules.cogs.quickdraw.cog import QuickdrawDuel
from bot_modules.duels.db import CHALLENGE_RESPONSE_SECONDS
from bot_modules.duels.views import CHALLENGE_TIMED_OUT_TEXT, ChallengeView
from bot_modules.services.games_db import GamesDb
from tests.fakes import FakeEconGamesBot, fake_interaction

GUILD = 9001
CH = 100


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


class RecordingBot(FakeEconGamesBot):
    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.views: list[tuple[object, int | None]] = []

    def add_view(self, view, *, message_id=None) -> None:
        self.views.append((view, message_id))


async def _pending(db: GamesDb, *, age: float, message_id: int | None = 555) -> int:
    gid = await qdb.create_game(db, GUILD, CH, 1, 2, None)
    await qdb.set_game_state(
        db, gid, "PENDING", message_id=message_id, created_at=time.time() - age,
    )
    return gid


# ── cog_load ──────────────────────────────────────────────────────────────────

async def test_live_pending_challenge_is_reattached_with_its_deadline(db, sync_db_path):
    bot = RecordingBot(db, sync_db_path, [1, 2])
    cog = QuickdrawDuel(bot)  # type: ignore[arg-type]
    gid = await _pending(db, age=60)

    assert await cog._reattach_pending_challenges() == 1

    (view, message_id), = bot.views
    assert isinstance(view, ChallengeView)
    assert message_id == 555
    assert view.game_id == gid
    assert view.target_id == 2
    assert view.is_persistent()  # add_view refuses anything else
    game = await qdb.get_game(db, gid)
    assert view.deadline == pytest.approx(game.created_at + CHALLENGE_RESPONSE_SECONDS)


@pytest.mark.parametrize(
    "age, message_id",
    [
        pytest.param(CHALLENGE_RESPONSE_SECONDS + 30, 555, id="already-expired"),
        pytest.param(60, None, id="no-card"),
    ],
)
async def test_dead_or_cardless_challenges_are_left_to_the_sweep(db, sync_db_path, age, message_id):
    bot = RecordingBot(db, sync_db_path, [1, 2])
    cog = QuickdrawDuel(bot)  # type: ignore[arg-type]
    await _pending(db, age=age, message_id=message_id)

    assert await cog._reattach_pending_challenges() == 0
    assert bot.views == []


@pytest.mark.parametrize("state", ["RESOLVED_NO_NICK", "NO_NICK_SET", "RESOLVED", "NICKED"])
async def test_a_result_card_inside_its_rematch_window_is_reattached(
    db, sync_db_path, monkeypatch, state
):
    """Every settled state's result card carries Run It Back for five
    minutes, so every one of them is re-attached on load — the fetch used to
    stop at RESOLVED / NICKED, and a wager-only game (always
    RESOLVED_NO_NICK) restarted into a dead button."""
    from bot_modules.duels.views import ResultView

    bot = RecordingBot(db, sync_db_path, [1, 2])
    cog = QuickdrawDuel(bot)  # type: ignore[arg-type]
    monkeypatch.setattr(cog._expire_loop, "start", lambda: None)
    gid = await qdb.create_game(db, GUILD, CH, 1, 2, None)
    await qdb.set_game_state(
        db, gid, state, winner_id=1, loser_id=2,
        resolved_at=time.time() - 30, result_message_id=700,
    )

    await cog.cog_load()

    (view, message_id), = bot.views
    assert isinstance(view, ResultView) and message_id == 700
    assert view.game_id == gid
    assert any(getattr(c, "custom_id", "") == f"rematch:{gid}" for c in view.children)


# ── the re-attached view ──────────────────────────────────────────────────────

def _view(deadline: float, calls: list):
    async def on_accept(interaction, game_id):
        calls.append(("accept", game_id))

    async def on_decline(interaction, game_id):
        calls.append(("decline", game_id))

    return ChallengeView(7, target_id=2, on_accept=on_accept, on_decline=on_decline, deadline=deadline)


async def test_reattached_view_still_accepts_inside_the_window():
    calls: list = []
    view = _view(time.time() + 120, calls)
    interaction = fake_interaction()
    interaction.user.id = 2
    await view._accept_callback(interaction)
    assert calls == [("accept", 7)]


@pytest.mark.parametrize("press", ["_accept_callback", "_decline_callback"])
async def test_reattached_view_refuses_a_press_past_the_deadline(press):
    """Persistent means no timeout of its own, so the deadline the card
    counted down to is enforced here — the same copy a late presser on the
    original card gets from the stale-state check."""
    calls: list = []
    view = _view(time.time() - 1, calls)
    interaction = fake_interaction()
    interaction.user.id = 2
    await getattr(view, press)(interaction)
    assert calls == []
    interaction.response.send_message.assert_awaited_once_with(
        CHALLENGE_TIMED_OUT_TEXT, ephemeral=True
    )


def test_fresh_view_keeps_its_own_timeout():
    async def _noop(interaction, game_id):
        pass

    view = ChallengeView(7, target_id=2, on_accept=_noop, on_decline=_noop)
    assert view.timeout == CHALLENGE_RESPONSE_SECONDS
    assert view.deadline is None
