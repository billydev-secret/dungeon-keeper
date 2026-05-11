"""Tests for CropEditorView button callbacks."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.veil_models import BoundingBox

GUILD_ID = 9001
VEIL_CHANNEL_ID = 8001
IMG_W, IMG_H = 500, 500

# Minimal fake JPEG header so discord.File doesn't complain
_FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20


def _box(x1: float = 100, y1: float = 100, x2: float = 300, y2: float = 300) -> BoundingBox:
    return BoundingBox(x1, y1, x2, y2)


def _make_view(crop_box: BoundingBox | None = None):  # type: ignore[return]
    from cogs.veil_cog import CropEditorView

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    bot.add_view = MagicMock()
    return CropEditorView(
        bot,
        image_bytes=b"fake",
        img_w=IMG_W,
        img_h=IMG_H,
        crop_box=crop_box or _box(),
        guild_id=GUILD_ID,
        veil_channel_id=VEIL_CHANNEL_ID,
        submitter_id=1001,
        answer_id=1001,
        difficulty="medium",
        candidate_count=1,
    )


@pytest.fixture
def inter() -> MagicMock:
    mock = MagicMock()
    mock.response = MagicMock()
    mock.response.edit_message = AsyncMock()
    mock.response.send_message = AsyncMock()
    mock.response.defer = AsyncMock()
    mock.followup = MagicMock()
    mock.followup.send = AsyncMock()
    mock.edit_original_response = AsyncMock()
    return mock


# ── Direction buttons ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_up_moves_box_up(inter: MagicMock) -> None:
    view = _make_view(_box(100, 200, 300, 400))
    original_y1 = view.crop_box.y1
    with patch("cogs.veil_cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_up(inter)
    assert view.crop_box.y1 < original_y1


@pytest.mark.asyncio
async def test_down_moves_box_down(inter: MagicMock) -> None:
    view = _make_view(_box(100, 100, 300, 300))
    original_y1 = view.crop_box.y1
    with patch("cogs.veil_cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_down(inter)
    assert view.crop_box.y1 > original_y1


@pytest.mark.asyncio
async def test_left_moves_box_left(inter: MagicMock) -> None:
    view = _make_view(_box(200, 100, 400, 300))
    original_x1 = view.crop_box.x1
    with patch("cogs.veil_cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_left(inter)
    assert view.crop_box.x1 < original_x1


@pytest.mark.asyncio
async def test_right_moves_box_right(inter: MagicMock) -> None:
    view = _make_view(_box(100, 100, 300, 300))
    original_x1 = view.crop_box.x1
    with patch("cogs.veil_cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_right(inter)
    assert view.crop_box.x1 > original_x1


# ── Zoom buttons ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_zoom_in_shrinks_box(inter: MagicMock) -> None:
    view = _make_view(_box(50, 50, 450, 450))  # 400×400, well above min_px=200
    original_w = view.crop_box.width
    with patch("cogs.veil_cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_zoom_in(inter)
    assert view.crop_box.width < original_w


@pytest.mark.asyncio
async def test_zoom_out_expands_box(inter: MagicMock) -> None:
    view = _make_view(_box(100, 100, 300, 300))  # 200×200
    original_w = view.crop_box.width
    with patch("cogs.veil_cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_zoom_out(inter)
    assert view.crop_box.width > original_w


# ── Edge clamping ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_up_clamps_at_top_edge(inter: MagicMock) -> None:
    view = _make_view(_box(0, 0, 200, 200))  # already at top
    with patch("cogs.veil_cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_up(inter)
    assert view.crop_box.y1 == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_down_clamps_at_bottom_edge(inter: MagicMock) -> None:
    view = _make_view(_box(300, 300, 500, 500))  # already at bottom
    with patch("cogs.veil_cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_down(inter)
    assert view.crop_box.y2 == pytest.approx(float(IMG_H))


# ── Cancel ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_edits_message_with_no_view(inter: MagicMock) -> None:
    view = _make_view()
    await view._on_cancel(inter)
    inter.response.edit_message.assert_called_once()
    call_kwargs = inter.response.edit_message.call_args.kwargs
    assert call_kwargs.get("view") is None


# ── Rerender ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rerender_calls_edit_message(inter: MagicMock) -> None:
    view = _make_view()
    with patch("cogs.veil_cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._rerender(inter)
    inter.response.edit_message.assert_called_once()


# ── Double-post prevention ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_double_post_sends_already_posted(inter: MagicMock) -> None:
    view = _make_view()
    view._posted = True
    await view._on_post(inter)
    inter.response.send_message.assert_called_once()
    msg = inter.response.send_message.call_args.args[0]
    assert "already" in msg.lower()


# ── Auto button ───────────────────────────────────────────────────────────────

def _make_view_with_candidates(*boxes: BoundingBox):  # type: ignore[return]
    from cogs.veil_cog import CropEditorView

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    bot.add_view = MagicMock()
    box_list = list(boxes)
    return CropEditorView(
        bot,
        image_bytes=b"fake",
        img_w=IMG_W,
        img_h=IMG_H,
        crop_box=box_list[0] if box_list else _box(),
        guild_id=GUILD_ID,
        veil_channel_id=VEIL_CHANNEL_ID,
        submitter_id=1001,
        answer_id=1001,
        difficulty="medium",
        candidate_count=len(box_list),
        candidate_boxes=box_list,
    )


@pytest.mark.asyncio
async def test_auto_first_press_snaps_to_first_candidate(inter: MagicMock) -> None:
    box0 = _box(10, 10, 100, 100)
    box1 = _box(200, 200, 400, 400)
    view = _make_view_with_candidates(box0, box1)
    view.crop_box = _box(300, 300, 490, 490)  # simulate user having panned away
    with patch("cogs.veil_cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_auto(inter)
    assert view.crop_box == box0


@pytest.mark.asyncio
async def test_auto_second_press_snaps_to_second_candidate(inter: MagicMock) -> None:
    box0 = _box(10, 10, 100, 100)
    box1 = _box(200, 200, 400, 400)
    view = _make_view_with_candidates(box0, box1)
    with patch("cogs.veil_cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_auto(inter)
        await view._on_auto(inter)
    assert view.crop_box == box1


@pytest.mark.asyncio
async def test_auto_wraps_from_last_to_first(inter: MagicMock) -> None:
    box0 = _box(10, 10, 100, 100)
    box1 = _box(200, 200, 400, 400)
    view = _make_view_with_candidates(box0, box1)
    with patch("cogs.veil_cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_auto(inter)  # → box0
        await view._on_auto(inter)  # → box1
        await view._on_auto(inter)  # → wraps back to box0
    assert view.crop_box == box0


@pytest.mark.asyncio
async def test_auto_label_is_always_plain_auto(inter: MagicMock) -> None:
    boxes = [_box(10, 10, 100, 100), _box(200, 200, 400, 400)]
    view = _make_view_with_candidates(*boxes)
    with patch("cogs.veil_cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_auto(inter)
        await view._on_auto(inter)
    assert view._auto_btn.label == "Auto"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_auto_noop_when_no_candidates(inter: MagicMock) -> None:
    view = _make_view()  # no candidate_boxes
    original_box = view.crop_box
    await view._on_auto(inter)
    assert view.crop_box == original_box
    inter.response.edit_message.assert_not_called()
