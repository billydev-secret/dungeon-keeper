"""Shared review-card rendering behind the pin and QOTD-sponsor views.

Both products show a mod a card with Approve/Deny buttons and re-render it
when the row moves. The mechanics are shared; the embed each builds is not,
so ``build_embed`` is injected rather than branched on.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.economy.view_helpers import edit_review_card, refresh_review_card


def _card(*, raises: Exception | None = None):
    card = MagicMock(spec=discord.Message)
    card.edit = AsyncMock(side_effect=raises)
    return card


def _ctx(row=None, *, read_raises: Exception | None = None):
    ctx = MagicMock()

    @contextmanager
    def _open_db():
        if read_raises:
            raise read_raises
        yield MagicMock()

    ctx.open_db = _open_db
    return ctx


def _embed(accent, settings, row):
    return discord.Embed(title=f"row {row['id']}")


# ── edit_review_card ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_card_is_edited_with_the_callers_embed():
    """Pins and sponsored questions render different embeds from different
    columns; only the edit-and-swallow mechanics are shared."""
    card = _card()
    await edit_review_card(
        card, None, None, {"id": 5}, build_embed=_embed, log_label="econ pin"
    )
    card.edit.assert_awaited_once()
    assert card.edit.await_args.kwargs["embed"].title == "row 5"
    assert card.edit.await_args.kwargs["view"] is None  # buttons retired


@pytest.mark.asyncio
async def test_no_card_is_not_an_error():
    """The card may have been deleted; the row is still resolved."""
    await edit_review_card(None, None, None, {"id": 5}, build_embed=_embed, log_label="x")


@pytest.mark.asyncio
async def test_a_failed_edit_is_swallowed(caplog):
    """Losing the card isn't worth raising over — the member was already told."""
    card = _card(raises=discord.HTTPException(MagicMock(status=500), "nope"))
    with caplog.at_level(logging.DEBUG, logger="dungeonkeeper.economy"):
        await edit_review_card(
            card, None, None, {"id": 5}, build_embed=_embed, log_label="econ pin"
        )
    assert "econ pin" in caplog.text


# ── refresh_review_card ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_row_is_reread_before_rendering():
    """The row can move underneath the card — resolved from the dashboard, or
    by a second mod. Re-reading is what stops a card claiming the wrong
    outcome."""
    card = _card()
    reads: list[int] = []

    def _read(conn, submission_id):
        reads.append(submission_id)
        return {"id": submission_id}

    await refresh_review_card(
        card, _ctx(), None, None, 9,
        read_row=_read, build_embed=_embed, log_label="econ pin",
    )
    assert reads == [9]
    assert card.edit.await_args.kwargs["embed"].title == "row 9"


@pytest.mark.asyncio
async def test_a_failed_read_leaves_the_card_alone(caplog):
    """Stale beats wrong-and-confident: don't blank a card we couldn't verify."""
    card = _card()
    with caplog.at_level(logging.DEBUG, logger="dungeonkeeper.economy"):
        await refresh_review_card(
            card, _ctx(read_raises=RuntimeError("db gone")), None, None, 9,
            read_row=lambda c, i: None, build_embed=_embed, log_label="econ pin",
        )
    card.edit.assert_not_awaited()
    assert "failed to reload" in caplog.text


@pytest.mark.asyncio
async def test_a_vanished_row_leaves_the_card_alone():
    """Deleted underneath us — nothing to re-render."""
    card = _card()
    await refresh_review_card(
        card, _ctx(), None, None, 9,
        read_row=lambda c, i: None, build_embed=_embed, log_label="x",
    )
    card.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_card_skips_the_read_entirely():
    reads: list[int] = []
    await refresh_review_card(
        None, _ctx(), None, None, 9,
        read_row=lambda c, i: reads.append(i), build_embed=_embed, log_label="x",
    )
    assert reads == []


@pytest.mark.asyncio
async def test_the_two_products_bind_their_own_label_and_embed():
    """pin_views and sponsor_views are partials over these, so the wiring is
    what could break rather than the logic."""
    from bot_modules.economy import pin_views, sponsor_views

    assert pin_views._edit_card.keywords["log_label"] == "econ pin"
    assert sponsor_views._edit_card.keywords["log_label"] == "econ sponsor"
    assert (
        pin_views._refresh_card.keywords["build_embed"]
        is not sponsor_views._refresh_card.keywords["build_embed"]
    )
