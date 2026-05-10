"""cog_load should re-register only unsolved rounds and apply a hard cap."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from db_utils import open_db
from services.veil_repo import insert_round, mark_round_solved


def _make_cog(db_path: Path) -> tuple[object, MagicMock]:
    """Return (cog, add_view_mock). Hand the mock back so tests don't have to
    re-derive it from cog.bot — keeps pyright off our backs about MethodType."""
    from cogs.veil_cog import VeilCog
    bot = MagicMock()
    bot.ctx.db_path = str(db_path)
    add_view = MagicMock()
    bot.add_view = add_view
    return VeilCog(bot), add_view


@pytest.mark.asyncio
async def test_cog_load_skips_solved_rounds(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        unsolved = insert_round(conn, guild_id=1, submitter_id=10, answer_id=10)
        solved = insert_round(conn, guild_id=1, submitter_id=11, answer_id=11)
        mark_round_solved(conn, solved, solver_id=99, guesses_to_solve=1, unique_guessers_to_solve=1)

    cog, add_view = _make_cog(sync_db_path)
    await cog.cog_load()  # type: ignore[attr-defined]

    assert add_view.call_count == 1
    registered_ids = [c.args[0].round_id for c in add_view.call_args_list]
    assert unsolved in registered_ids
    assert solved not in registered_ids


@pytest.mark.asyncio
async def test_cog_load_caps_total_views(sync_db_path: Path, monkeypatch):
    import cogs.veil_cog as veil_cog
    monkeypatch.setattr(veil_cog, "_COG_LOAD_VIEW_CAP", 3)

    with open_db(sync_db_path) as conn:
        for i in range(10):
            insert_round(conn, guild_id=1, submitter_id=i, answer_id=i)

    cog, add_view = _make_cog(sync_db_path)
    await cog.cog_load()  # type: ignore[attr-defined]

    assert add_view.call_count == 3


@pytest.mark.asyncio
async def test_cog_load_passes_current_guess_count_to_view(sync_db_path: Path):
    """Reconstructed GameViews must carry the round's current guess count,
    so the chip label reflects reality after a restart."""
    from services.veil_repo import insert_guess

    with open_db(sync_db_path) as conn:
        rid = insert_round(conn, guild_id=1, submitter_id=10, answer_id=10)
        for i in range(3):
            insert_guess(conn, round_id=rid, guesser_id=100 + i,
                         guessed_user_id=999, correct=False)

    cog, add_view = _make_cog(sync_db_path)
    await cog.cog_load()  # type: ignore[attr-defined]

    assert add_view.call_count == 1
    reconstructed_view = add_view.call_args_list[0].args[0]
    labels = [c.label for c in reconstructed_view.children]
    assert "Guesses: 3" in labels
