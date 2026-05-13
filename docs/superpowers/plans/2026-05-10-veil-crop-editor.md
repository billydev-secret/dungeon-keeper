# Veil Crop Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the pipeline-controlled crop selection in `/veil submit` with an interactive Discord UI that shows the full image with a moveable/zoomable crop box, using a 3×3 button grid.

**Architecture:** Two new pure functions (`move_crop_box`, `zoom_crop_box`) live in `services/veil_pipeline.py` alongside the existing geometry helpers. A new `render_crop_editor` function in `services/veil_crop_renderer.py` renders the full image scaled to 1280px with a red rectangle overlay. `CropEditorView` in `cogs/veil_cog.py` replaces `SubmitPreviewView` — on each button press it updates `self.crop_box`, calls `render_crop_editor` via `asyncio.to_thread`, and edits the ephemeral message. On ✓ it calls `render_crop` to produce the final crop and posts to the game channel.

**Tech Stack:** discord.py persistent views, asyncio.to_thread, PIL/Pillow (lazy import), pytest-asyncio

---

## File map

| File | Change |
|---|---|
| `services/veil_pipeline.py` | Add `move_crop_box`, `zoom_crop_box` |
| `services/veil_crop_renderer.py` | Add `render_crop_editor` |
| `cogs/veil_cog.py` | Replace `SubmitPreviewView` with `CropEditorView`; update imports; update `veil_submit` |
| `tests/unit/test_veil_pipeline.py` | Add geometry tests (existing file) |
| `tests/unit/test_veil_crop_renderer.py` | Add overlay renderer tests (existing file) |
| `tests/cogs/test_veil_crop_editor.py` | New — CropEditorView button callback tests |
| `tests/cogs/test_veil_submit.py` | Update two affected tests |

---

## Task 1: Geometry helpers — `move_crop_box` and `zoom_crop_box`

**Files:**
- Modify: `services/veil_pipeline.py` (append two functions after `enforce_min_size`)
- Modify: `tests/unit/test_veil_pipeline.py` (append tests at the end)

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_veil_pipeline.py`:

```python
from services.veil_pipeline import (
    HIGH_INTEREST_WEIGHT,
    LOW_INTEREST_WEIGHT,
    apply_label_weights,
    compute_padded_crop,
    enforce_min_size,
    filter_candidates,
    iou,
    move_crop_box,
    zoom_crop_box,
)


# ── move_crop_box ─────────────────────────────────────────────────────────────

def test_move_crop_box_shifts_right():
    box = _bb(100, 100, 300, 300)
    r = move_crop_box(box, 50.0, 0.0, 500, 500)
    assert r.x1 == pytest.approx(150.0)
    assert r.x2 == pytest.approx(350.0)


def test_move_crop_box_shifts_down():
    box = _bb(100, 100, 300, 300)
    r = move_crop_box(box, 0.0, 40.0, 500, 500)
    assert r.y1 == pytest.approx(140.0)
    assert r.y2 == pytest.approx(340.0)


def test_move_crop_box_clamps_left_edge():
    # x1=10, width=200; dx=-50 would give x1=-40 — clamp to 0
    r = move_crop_box(_bb(10, 100, 210, 300), -50.0, 0.0, 500, 500)
    assert r.x1 == pytest.approx(0.0)
    assert r.width == pytest.approx(200.0)


def test_move_crop_box_clamps_right_edge():
    # x2=450, width=100; dx=100 would give x2=550 — clamp so x2==500
    r = move_crop_box(_bb(350, 100, 450, 300), 100.0, 0.0, 500, 500)
    assert r.x2 == pytest.approx(500.0)
    assert r.width == pytest.approx(100.0)


def test_move_crop_box_clamps_top_edge():
    r = move_crop_box(_bb(100, 10, 300, 110), 0.0, -50.0, 500, 500)
    assert r.y1 == pytest.approx(0.0)


def test_move_crop_box_clamps_bottom_edge():
    r = move_crop_box(_bb(100, 400, 300, 490), 0.0, 50.0, 500, 500)
    assert r.y2 == pytest.approx(500.0)


def test_move_crop_box_preserves_dimensions():
    box = _bb(50, 50, 150, 250)
    r = move_crop_box(box, 30.0, -20.0, 500, 500)
    assert r.width == pytest.approx(100.0)
    assert r.height == pytest.approx(200.0)


# ── zoom_crop_box ─────────────────────────────────────────────────────────────

def test_zoom_in_shrinks_box():
    box = _bb(100, 100, 300, 300)  # 200x200
    r = zoom_crop_box(box, 0.8, 500, 500)
    assert r.width < 200.0
    assert r.height < 200.0


def test_zoom_out_grows_box():
    box = _bb(100, 100, 300, 300)  # 200x200
    r = zoom_crop_box(box, 1.25, 500, 500)
    assert r.width > 200.0
    assert r.height > 200.0


def test_zoom_preserves_center():
    box = _bb(100, 100, 300, 300)  # center at (200, 200)
    r = zoom_crop_box(box, 0.8, 500, 500)
    assert (r.x1 + r.x2) / 2 == pytest.approx(200.0)
    assert (r.y1 + r.y2) / 2 == pytest.approx(200.0)


def test_zoom_in_respects_min_px():
    box = _bb(200, 200, 220, 220)  # 20x20 — well below min
    r = zoom_crop_box(box, 0.8, 500, 500, min_px=200)
    assert r.width >= 200.0
    assert r.height >= 200.0


def test_zoom_out_clamps_to_image_bounds():
    box = _bb(0, 0, 500, 500)  # already full image
    r = zoom_crop_box(box, 1.25, 500, 500)
    assert r.x1 >= 0.0
    assert r.y1 >= 0.0
    assert r.x2 <= 500.0
    assert r.y2 <= 500.0


def test_zoom_result_is_valid_box():
    box = _bb(100, 100, 200, 200)
    r = zoom_crop_box(box, 0.5, 300, 300)
    assert r.x1 < r.x2
    assert r.y1 < r.y2
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/unit/test_veil_pipeline.py -k "move_crop_box or zoom_crop_box" -v
```

Expected: ImportError or FAILED (names not yet defined)

- [ ] **Step 3: Implement `move_crop_box` and `zoom_crop_box`**

In `services/veil_pipeline.py`, append after the `enforce_min_size` function (after line 208):

```python
def move_crop_box(
    box: BoundingBox,
    dx: float,
    dy: float,
    img_w: int,
    img_h: int,
) -> BoundingBox:
    """Translate box by (dx, dy) and clamp so it stays within image bounds.

    Width and height are preserved — the box shifts then clamps so neither
    edge exceeds [0, img_w] / [0, img_h].
    """
    w, h = box.width, box.height
    new_x1 = max(0.0, min(box.x1 + dx, float(img_w) - w))
    new_y1 = max(0.0, min(box.y1 + dy, float(img_h) - h))
    return BoundingBox(new_x1, new_y1, new_x1 + w, new_y1 + h)


def zoom_crop_box(
    box: BoundingBox,
    factor: float,
    img_w: int,
    img_h: int,
    min_px: int = 200,
) -> BoundingBox:
    """Scale box around its center by factor, enforce min_px, clamp to image.

    factor < 1 zooms in (shrinks the visible box); factor > 1 zooms out.
    """
    cx = (box.x1 + box.x2) / 2.0
    cy = (box.y1 + box.y2) / 2.0
    half_w = max(float(min_px) / 2.0, min(box.width * factor / 2.0, float(img_w) / 2.0))
    half_h = max(float(min_px) / 2.0, min(box.height * factor / 2.0, float(img_h) / 2.0))
    return BoundingBox(
        max(0.0, cx - half_w),
        max(0.0, cy - half_h),
        min(float(img_w), cx + half_w),
        min(float(img_h), cy + half_h),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/unit/test_veil_pipeline.py -v
```

Expected: All PASS (including the new geometry tests)

- [ ] **Step 5: Commit**

```
git add services/veil_pipeline.py tests/unit/test_veil_pipeline.py
git commit -m "feat(veil): add move_crop_box and zoom_crop_box geometry helpers"
```

---

## Task 2: Overlay renderer — `render_crop_editor`

**Files:**
- Modify: `services/veil_crop_renderer.py` (append function)
- Modify: `tests/unit/test_veil_crop_renderer.py` (append tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_veil_crop_renderer.py`:

```python
# ── render_crop_editor ────────────────────────────────────────────────────────

def test_render_crop_editor_returns_jpeg_bytes():
    from services.veil_crop_renderer import render_crop_editor
    image_bytes = _make_jpeg(400, 400)
    box = BoundingBox(x1=50, y1=50, x2=300, y2=300)
    result = render_crop_editor(image_bytes, box)
    assert isinstance(result, bytes)
    assert result[:2] == b"\xff\xd8", "Expected JPEG magic bytes"


def test_render_crop_editor_scales_down_large_image():
    from services.veil_crop_renderer import render_crop_editor
    image_bytes = _make_jpeg(2000, 2000)
    box = BoundingBox(x1=500, y1=500, x2=1500, y2=1500)
    result = render_crop_editor(image_bytes, box, max_display_px=1280)
    img = Image.open(io.BytesIO(result))
    assert max(img.size) <= 1280


def test_render_crop_editor_does_not_upscale_small_image():
    from services.veil_crop_renderer import render_crop_editor
    image_bytes = _make_jpeg(400, 300)
    box = BoundingBox(x1=50, y1=50, x2=200, y2=200)
    result = render_crop_editor(image_bytes, box, max_display_px=1280)
    img = Image.open(io.BytesIO(result))
    assert img.size == (400, 300)


def test_render_crop_editor_full_image_box_does_not_error():
    from services.veil_crop_renderer import render_crop_editor
    image_bytes = _make_jpeg(200, 200)
    box = BoundingBox(x1=0, y1=0, x2=200, y2=200)
    result = render_crop_editor(image_bytes, box)
    assert len(result) > 0
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/unit/test_veil_crop_renderer.py -k "render_crop_editor" -v
```

Expected: ImportError — `render_crop_editor` not yet defined

- [ ] **Step 3: Implement `render_crop_editor`**

In `services/veil_crop_renderer.py`, append after `render_crop`:

```python
def render_crop_editor(
    image_bytes: bytes,
    crop_box: BoundingBox,
    *,
    max_display_px: int = 1280,
    jpeg_quality: int = 80,
) -> bytes:
    """Render the full image scaled to max_display_px with crop_box drawn as a red rectangle.

    Does not upscale images that are already smaller than max_display_px.
    """
    from PIL import Image, ImageDraw  # type: ignore[import-untyped]  # noqa: PLC0415

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    orig_w, orig_h = img.size

    scale = min(max_display_px / max(orig_w, orig_h, 1), 1.0)
    display_w = max(1, int(orig_w * scale))
    display_h = max(1, int(orig_h * scale))
    display_img = img.resize((display_w, display_h), Image.LANCZOS)

    draw = ImageDraw.Draw(display_img)
    draw.rectangle(
        [int(crop_box.x1 * scale), int(crop_box.y1 * scale),
         int(crop_box.x2 * scale), int(crop_box.y2 * scale)],
        outline=(255, 50, 50),
        width=3,
    )

    buf = io.BytesIO()
    display_img.save(buf, format="JPEG", quality=jpeg_quality)
    return buf.getvalue()
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/unit/test_veil_crop_renderer.py -v
```

Expected: All PASS

- [ ] **Step 5: Commit**

```
git add services/veil_crop_renderer.py tests/unit/test_veil_crop_renderer.py
git commit -m "feat(veil): add render_crop_editor overlay renderer"
```

---

## Task 3: `CropEditorView` class and button callback tests

**Files:**
- Create: `tests/cogs/test_veil_crop_editor.py`
- Modify: `cogs/veil_cog.py` (add class, update imports, add constants)

### Sub-task 3a: Tests first

- [ ] **Step 1: Write the failing tests**

Create `tests/cogs/test_veil_crop_editor.py`:

```python
"""Tests for CropEditorView button callbacks."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.veil_models import BoundingBox


GUILD_ID = 9001
CHANNEL_ID = 8001


def _make_view(
    img_w: int = 500,
    img_h: int = 500,
    crop_box: BoundingBox | None = None,
) -> "CropEditorView":
    from cogs.veil_cog import CropEditorView

    bot = MagicMock()
    bot.ctx.db_path = MagicMock()
    if crop_box is None:
        crop_box = BoundingBox(150.0, 150.0, 350.0, 350.0)  # 200x200, centered at (250,250)
    return CropEditorView(
        bot,
        image_bytes=b"fake",
        img_w=img_w,
        img_h=img_h,
        crop_box=crop_box,
        guild_id=GUILD_ID,
        veil_channel_id=CHANNEL_ID,
        submitter_id=1001,
        answer_id=1001,
        difficulty="medium",
        candidate_count=3,
    )


def _make_interaction() -> MagicMock:
    from tests.fakes import fake_interaction, FakeGuild

    guild = FakeGuild(id=GUILD_ID)
    i = fake_interaction(guild=guild)
    i.guild.id = GUILD_ID
    return i


# ── move buttons ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_up_moves_crop_upward():
    view = _make_view()
    initial_y1 = view.crop_box.y1
    with patch("cogs.veil_cog.render_crop_editor", return_value=b"\xff\xd8fake"):
        await view._on_up(_make_interaction())
    assert view.crop_box.y1 < initial_y1


@pytest.mark.asyncio
async def test_down_moves_crop_downward():
    view = _make_view()
    initial_y1 = view.crop_box.y1
    with patch("cogs.veil_cog.render_crop_editor", return_value=b"\xff\xd8fake"):
        await view._on_down(_make_interaction())
    assert view.crop_box.y1 > initial_y1


@pytest.mark.asyncio
async def test_left_moves_crop_leftward():
    view = _make_view()
    initial_x1 = view.crop_box.x1
    with patch("cogs.veil_cog.render_crop_editor", return_value=b"\xff\xd8fake"):
        await view._on_left(_make_interaction())
    assert view.crop_box.x1 < initial_x1


@pytest.mark.asyncio
async def test_right_moves_crop_rightward():
    view = _make_view()
    initial_x1 = view.crop_box.x1
    with patch("cogs.veil_cog.render_crop_editor", return_value=b"\xff\xd8fake"):
        await view._on_right(_make_interaction())
    assert view.crop_box.x1 > initial_x1


# ── zoom buttons ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_zoom_in_shrinks_crop():
    view = _make_view()
    initial_w = view.crop_box.width
    with patch("cogs.veil_cog.render_crop_editor", return_value=b"\xff\xd8fake"):
        await view._on_zoom_in(_make_interaction())
    assert view.crop_box.width < initial_w


@pytest.mark.asyncio
async def test_zoom_out_grows_crop():
    view = _make_view()
    initial_w = view.crop_box.width
    with patch("cogs.veil_cog.render_crop_editor", return_value=b"\xff\xd8fake"):
        await view._on_zoom_out(_make_interaction())
    assert view.crop_box.width > initial_w


# ── boundary clamping ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_move_at_top_left_corner_clamps():
    view = _make_view(crop_box=BoundingBox(0.0, 0.0, 200.0, 200.0))
    with patch("cogs.veil_cog.render_crop_editor", return_value=b"\xff\xd8fake"):
        await view._on_up(_make_interaction())
        await view._on_left(_make_interaction())
    assert view.crop_box.x1 == pytest.approx(0.0)
    assert view.crop_box.y1 == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_move_at_bottom_right_corner_clamps():
    view = _make_view(500, 500, crop_box=BoundingBox(300.0, 300.0, 500.0, 500.0))
    with patch("cogs.veil_cog.render_crop_editor", return_value=b"\xff\xd8fake"):
        await view._on_down(_make_interaction())
        await view._on_right(_make_interaction())
    assert view.crop_box.x2 == pytest.approx(500.0)
    assert view.crop_box.y2 == pytest.approx(500.0)


# ── cancel ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_stops_view_and_edits_message():
    view = _make_view()
    interaction = _make_interaction()
    await view._on_cancel(interaction)
    interaction.response.edit_message.assert_awaited_once()
    # Check the message content signals cancellation
    kwargs = interaction.response.edit_message.call_args.kwargs
    assert "cancel" in (kwargs.get("content") or "").lower()


# ── rerender calls edit_message ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rerender_calls_edit_message_with_file():
    view = _make_view()
    interaction = _make_interaction()
    with patch("cogs.veil_cog.render_crop_editor", return_value=b"\xff\xd8fake"):
        await view._rerender(interaction)
    interaction.response.edit_message.assert_awaited_once()


# ── post prevents double-fire ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_post_second_call_is_rejected():
    view = _make_view()
    view._posted = True  # simulate already posted
    interaction = _make_interaction()
    await view._on_post(interaction)
    # Should respond with "already posted" message, not defer
    interaction.response.send_message.assert_awaited_once()
    interaction.response.defer.assert_not_awaited()
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/cogs/test_veil_crop_editor.py -v
```

Expected: ImportError — `CropEditorView` not yet defined

### Sub-task 3b: Implement `CropEditorView`

- [ ] **Step 3: Update imports in `cogs/veil_cog.py`**

Replace the current single import from `services.veil_pipeline`:
```python
from services.veil_pipeline import run_pipeline
```
with:
```python
from services.veil_crop_renderer import render_crop, render_crop_editor
from services.veil_pipeline import (
    compute_padded_crop,
    enforce_min_size,
    move_crop_box,
    run_pipeline,
    zoom_crop_box,
)
```

Also add `BoundingBox` to the existing `services.veil_models` import:
```python
from services.veil_models import BoundingBox, VeilConfig, VeilGuess, VeilRound
```

- [ ] **Step 4: Add constants just before `SubmitPreviewView`**

After the `SELECT_TIMEOUT_SECONDS = 60` line, add:

```python
CROP_EDITOR_ZOOM_IN: float = 0.8    # shrinks box (zoom into subject)
CROP_EDITOR_ZOOM_OUT: float = 1.25  # grows box (zoom out)
```

- [ ] **Step 5: Add `CropEditorView` class**

Add this class immediately after `SubmitPreviewView` (before the sticky-prompt section). It will be deleted in Task 4; for now add it so tests can import it:

```python
class CropEditorView(discord.ui.View):
    """Ephemeral interactive crop editor.

    Shows the full image scaled to ~1280px with a red rectangle overlay.
    The 3×3 button grid lets the submitter move and zoom the box, then post.

    Layout:
        Row 0: 🔍+  ↑   🔍−
        Row 1: ←    ✓   →
        Row 2: ·    ↓   ✗   (· is a disabled visual placeholder)
    """

    def __init__(
        self,
        bot: "Bot",
        image_bytes: bytes,
        img_w: int,
        img_h: int,
        crop_box: BoundingBox,
        guild_id: int,
        veil_channel_id: int,
        *,
        submitter_id: int,
        answer_id: int,
        difficulty: str,
        candidate_count: int,
        veil_role_id: int = 0,
        original_ext: str = ".jpg",
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.image_bytes = image_bytes
        self.img_w = img_w
        self.img_h = img_h
        self.crop_box = crop_box
        self.guild_id = guild_id
        self.veil_channel_id = veil_channel_id
        self._submitter_id = submitter_id
        self._answer_id = answer_id
        self._difficulty = difficulty
        self._candidate_count = candidate_count
        self._veil_role_id = veil_role_id
        self._original_ext = original_ext
        self._post_lock = asyncio.Lock()
        self._posted = False
        self._step_x = img_w / 5.0
        self._step_y = img_h / 5.0

        # Row 0
        zoom_in_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="🔍+", style=discord.ButtonStyle.secondary, row=0
        )
        zoom_in_btn.callback = self._on_zoom_in
        self.add_item(zoom_in_btn)

        up_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="↑", style=discord.ButtonStyle.secondary, row=0
        )
        up_btn.callback = self._on_up
        self.add_item(up_btn)

        zoom_out_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="🔍−", style=discord.ButtonStyle.secondary, row=0
        )
        zoom_out_btn.callback = self._on_zoom_out
        self.add_item(zoom_out_btn)

        # Row 1
        left_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="←", style=discord.ButtonStyle.secondary, row=1
        )
        left_btn.callback = self._on_left
        self.add_item(left_btn)

        self._submit_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="✓", style=discord.ButtonStyle.success, row=1
        )
        self._submit_btn.callback = self._on_post
        self.add_item(self._submit_btn)

        right_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="→", style=discord.ButtonStyle.secondary, row=1
        )
        right_btn.callback = self._on_right
        self.add_item(right_btn)

        # Row 2
        placeholder_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="·", style=discord.ButtonStyle.secondary, disabled=True, row=2
        )
        self.add_item(placeholder_btn)

        down_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="↓", style=discord.ButtonStyle.secondary, row=2
        )
        down_btn.callback = self._on_down
        self.add_item(down_btn)

        cancel_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="✗", style=discord.ButtonStyle.danger, row=2
        )
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    async def _rerender(self, interaction: discord.Interaction) -> None:
        editor_bytes = await asyncio.to_thread(
            render_crop_editor, self.image_bytes, self.crop_box
        )
        file = discord.File(io.BytesIO(editor_bytes), filename="editor.jpg")
        embed = discord.Embed(
            title="Position the crop box",
            description="↑↓←→ to move · 🔍 to zoom · ✓ to post",
        ).set_image(url="attachment://editor.jpg")
        await interaction.response.edit_message(embed=embed, attachments=[file], view=self)

    async def _on_up(self, interaction: discord.Interaction) -> None:
        self.crop_box = move_crop_box(self.crop_box, 0.0, -self._step_y, self.img_w, self.img_h)
        await self._rerender(interaction)

    async def _on_down(self, interaction: discord.Interaction) -> None:
        self.crop_box = move_crop_box(self.crop_box, 0.0, self._step_y, self.img_w, self.img_h)
        await self._rerender(interaction)

    async def _on_left(self, interaction: discord.Interaction) -> None:
        self.crop_box = move_crop_box(self.crop_box, -self._step_x, 0.0, self.img_w, self.img_h)
        await self._rerender(interaction)

    async def _on_right(self, interaction: discord.Interaction) -> None:
        self.crop_box = move_crop_box(self.crop_box, self._step_x, 0.0, self.img_w, self.img_h)
        await self._rerender(interaction)

    async def _on_zoom_in(self, interaction: discord.Interaction) -> None:
        self.crop_box = zoom_crop_box(self.crop_box, CROP_EDITOR_ZOOM_IN, self.img_w, self.img_h)
        await self._rerender(interaction)

    async def _on_zoom_out(self, interaction: discord.Interaction) -> None:
        self.crop_box = zoom_crop_box(self.crop_box, CROP_EDITOR_ZOOM_OUT, self.img_w, self.img_h)
        await self._rerender(interaction)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Submission cancelled.", embed=None, attachments=[], view=None
        )

    async def _on_post(self, interaction: discord.Interaction) -> None:
        assert interaction.guild
        async with self._post_lock:
            if self._posted:
                await interaction.response.send_message("Already posted.", ephemeral=True)
                return
            self._posted = True
            self._submit_btn.disabled = True

        await interaction.response.defer(ephemeral=True)

        veil_channel = interaction.guild.get_channel(self.veil_channel_id)
        if veil_channel is None or not isinstance(
            veil_channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread)
        ):
            await interaction.followup.send(
                "Veil channel not found — ask an admin to check the config.", ephemeral=True
            )
            return

        if hasattr(veil_channel, "is_nsfw") and not veil_channel.is_nsfw():
            await interaction.followup.send(
                f"{veil_channel.mention} is no longer NSFW-flagged. "
                "Veil refuses to post explicit content in non-age-gated channels.",
                ephemeral=True,
            )
            return

        db_path = self.bot.ctx.db_path
        round_id = await asyncio.to_thread(
            _do_insert_round,
            db_path,
            guild_id=self.guild_id,
            submitter_id=self._submitter_id,
            answer_id=self._answer_id,
            channel_id=self.veil_channel_id,
            difficulty=self._difficulty,
            allow_reuse=False,
            candidate_count=self._candidate_count,
        )

        if self.image_bytes:
            orig_path = _VEIL_ORIG_DIR / f"{round_id}{self._original_ext}"

            def _write_original() -> None:
                _VEIL_ORIG_DIR.mkdir(parents=True, exist_ok=True)
                orig_path.write_bytes(self.image_bytes)

            await asyncio.to_thread(_write_original)
            await asyncio.to_thread(_do_set_original_path, db_path, round_id, str(orig_path))

        crop_bytes = await asyncio.to_thread(render_crop, self.image_bytes, self.crop_box)
        crop_file = discord.File(io.BytesIO(crop_bytes), filename="SPOILER_veil_crop.jpg")
        game_view = GameView(self.bot, round_id)
        self.bot.add_view(game_view)
        role_ping = f"<@&{self._veil_role_id}>" if self._veil_role_id else None
        game_msg = await veil_channel.send(
            content=role_ping,
            embed=_game_embed(round_id),
            file=crop_file,
            view=game_view,
        )

        crop_url = game_msg.attachments[0].url if game_msg.attachments else ""
        await asyncio.to_thread(
            _do_update_round_message,
            db_path,
            round_id,
            game_msg.id,
            crop_url,
            "",
        )

        await asyncio.to_thread(
            _do_audit, db_path,
            guild_id=self.guild_id, actor_id=self._submitter_id,
            action="submit", round_id=round_id,
            details={"difficulty": self._difficulty, "rerolls": 0},
        )

        await interaction.edit_original_response(
            content=f"✅ Posted to {veil_channel.mention}!",
            view=self,
        )

        try:
            await _repost_prompt(self.bot, veil_channel, self.guild_id)
        except Exception:
            log.exception("veil: prompt repost after game post failed for guild %d", self.guild_id)
```

- [ ] **Step 6: Run all veil tests to verify they pass**

```
pytest tests/cogs/test_veil_crop_editor.py tests/unit/test_veil_pipeline.py tests/unit/test_veil_crop_renderer.py -v
```

Expected: All PASS

- [ ] **Step 7: Commit**

```
git add cogs/veil_cog.py tests/cogs/test_veil_crop_editor.py
git commit -m "feat(veil): add CropEditorView interactive crop editor"
```

---

## Task 4: Wire `veil_submit` to `CropEditorView` and remove `SubmitPreviewView`

**Files:**
- Modify: `cogs/veil_cog.py` (update `veil_submit`, delete `SubmitPreviewView`)
- Modify: `tests/cogs/test_veil_submit.py` (update two affected tests)

- [ ] **Step 1: Update `test_submit_success_sends_ephemeral_preview`**

The test needs to patch `render_crop_editor` (newly called in the submit path). Find the test and replace it:

```python
@pytest.mark.asyncio
async def test_submit_success_sends_ephemeral_preview(sync_db_path: Path):
    """Full happy-path: pipeline returns candidates, ephemeral editor sent to submitter.

    The veil channel is NOT posted to at submit time — that happens only when
    the submitter clicks ✓ in the editor view.
    """
    import io as _io
    from PIL import Image
    from services.veil_models import Detection, BoundingBox, PipelineResult

    buf = _io.BytesIO()
    Image.new("RGB", (500, 500)).save(buf, format="JPEG")
    img_bytes = buf.getvalue()

    det = Detection(label="BREAST", score=0.9, box=BoundingBox(10, 10, 100, 100))
    fake_result = PipelineResult(candidates=[det], crops=[b"fake-crop-jpeg"])

    member = _veil_member()
    guild = _guild(member)

    fake_channel = MagicMock(spec=discord.TextChannel)
    fake_channel.send = AsyncMock(return_value=_fake_game_message())
    guild.channels[VEIL_CHANNEL_ID] = fake_channel

    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)
    interaction.guild.get_channel = lambda cid: guild.channels.get(cid)
    interaction.user.id = member.id

    cog = _make_cog(str(sync_db_path))
    with patch("cogs.veil_cog._load_config", return_value=_cfg()):
        with patch("cogs.veil_cog.run_pipeline", return_value=fake_result):
            with patch("cogs.veil_cog.render_crop_editor", return_value=b"\xff\xd8fake"):
                await _submit(cog, interaction, _attachment(read_return=img_bytes))

    # Channel post deferred until the user clicks ✓ — not called here.
    fake_channel.send.assert_not_called()

    # Ephemeral editor sent to submitter
    interaction.followup.send.assert_called_once()
    call_kwargs = interaction.followup.send.call_args
    assert call_kwargs.kwargs.get("ephemeral") is True
```

- [ ] **Step 2: Update `test_on_post_reposts_prompt_after_game_message`**

Replace the test (which currently uses `SubmitPreviewView`) with an equivalent using `CropEditorView`:

```python
@pytest.mark.asyncio
async def test_on_post_reposts_prompt_after_game_message():
    """After posting a game round, _repost_prompt is called to move the
    sticky status bar below the new round."""
    from cogs.veil_cog import CropEditorView
    from services.veil_models import BoundingBox

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    bot.add_view = MagicMock()

    fake_channel = MagicMock(spec=discord.TextChannel)
    fake_channel.is_nsfw = MagicMock(return_value=True)
    fake_channel.send = AsyncMock(return_value=_fake_game_message())

    guild = FakeGuild(id=GUILD_ID)
    guild.channels[VEIL_CHANNEL_ID] = fake_channel

    interaction = fake_interaction(guild=guild)
    interaction.guild.get_channel = lambda cid: guild.channels.get(cid)

    view = CropEditorView(
        bot,
        image_bytes=b"",
        img_w=500,
        img_h=500,
        crop_box=BoundingBox(0.0, 0.0, 500.0, 500.0),
        guild_id=GUILD_ID,
        veil_channel_id=VEIL_CHANNEL_ID,
        submitter_id=1001,
        answer_id=1001,
        difficulty="medium",
        candidate_count=1,
    )

    with patch("cogs.veil_cog._do_insert_round", return_value=42), \
         patch("cogs.veil_cog._do_update_round_message"), \
         patch("cogs.veil_cog._do_audit"), \
         patch("cogs.veil_cog.render_crop", return_value=b"\xff\xd8fake"), \
         patch("cogs.veil_cog._repost_prompt", new_callable=AsyncMock) as mock_repost:
        await view._on_post(interaction)

    mock_repost.assert_awaited_once_with(bot, fake_channel, GUILD_ID)
```

- [ ] **Step 3: Run the test file to confirm the two tests now fail (expected — `CropEditorView` exists but `veil_submit` still creates `SubmitPreviewView`)**

```
pytest tests/cogs/test_veil_submit.py -v
```

Note the failures — they should be about `SubmitPreviewView` still being used in `veil_submit` (for `test_submit_success_sends_ephemeral_preview`) and the import change (for `test_on_post_reposts_prompt_after_game_message`).

- [ ] **Step 4: Update `veil_submit` in `cogs/veil_cog.py`**

In the `veil_submit` method:

**4a.** Change the dimension capture line from:
```python
dim_ok, *_ = await asyncio.to_thread(
    _validate_dimensions, image_bytes, config.min_image_dimension_px
)
```
to:
```python
dim_ok, img_w, img_h = await asyncio.to_thread(
    _validate_dimensions, image_bytes, config.min_image_dimension_px
)
```

**4b.** Change the empty-pipeline check from:
```python
if not pipeline_result.crops:
```
to:
```python
if not pipeline_result.candidates:
```

**4c.** Replace everything from `preview_file = discord.File(...)` through the end of the method with:

```python
        top_det = max(pipeline_result.candidates, key=lambda d: d.score)
        padded = compute_padded_crop(top_det.box, config.crop_difficulty, img_w, img_h)
        min_sized = enforce_min_size(padded)
        initial_crop = BoundingBox(
            max(0.0, min_sized.x1),
            max(0.0, min_sized.y1),
            min(float(img_w), min_sized.x2),
            min(float(img_h), min_sized.y2),
        )

        editor_bytes = await asyncio.to_thread(render_crop_editor, image_bytes, initial_crop)
        editor_file = discord.File(io.BytesIO(editor_bytes), filename="editor.jpg")
        editor_embed = discord.Embed(
            title="Position the crop box",
            description="↑↓←→ to move · 🔍 to zoom · ✓ to post",
        ).set_image(url="attachment://editor.jpg")
        original_ext = (Path(image.filename).suffix or ".jpg").lower()
        await interaction.followup.send(
            embed=editor_embed,
            file=editor_file,
            view=CropEditorView(
                self.bot,
                image_bytes,
                img_w,
                img_h,
                initial_crop,
                interaction.guild.id,
                config.veil_channel_id,
                submitter_id=interaction.user.id,
                answer_id=interaction.user.id,
                difficulty=config.crop_difficulty,
                candidate_count=len(pipeline_result.candidates),
                veil_role_id=config.veil_role_id,
                original_ext=original_ext,
            ),
            ephemeral=True,
        )
```

- [ ] **Step 5: Delete `SubmitPreviewView`**

Remove the entire `SubmitPreviewView` class (from `class SubmitPreviewView` through its closing method `_on_post`). It is fully replaced by `CropEditorView`.

- [ ] **Step 6: Run all veil tests**

```
pytest tests/cogs/test_veil_submit.py tests/cogs/test_veil_crop_editor.py tests/unit/test_veil_pipeline.py tests/unit/test_veil_crop_renderer.py -v
```

Expected: All PASS

- [ ] **Step 7: Run full test suite**

```
pytest --tb=short -q
```

Expected: Green. Any failures should be investigated before committing.

- [ ] **Step 8: Commit**

```
git add cogs/veil_cog.py tests/cogs/test_veil_submit.py
git commit -m "feat(veil): replace SubmitPreviewView with interactive CropEditorView"
```

---

## Self-review

**Spec coverage:**
- ✅ 3×3 button grid (🔍+/↑/🔍− / ←/✓/→ / ·/↓/✗)
- ✅ Move: step = image_dim / 5 → 5 presses crosses the image
- ✅ Zoom in/out: 0.8× / 1.25× factor around center
- ✅ Submit (✓): renders final crop, posts to game channel
- ✅ Cancel (✗): dismisses with message
- ✅ Disabled placeholder bottom-left
- ✅ Full image overlay preview re-renders on each press
- ✅ Timeout 5 min (no post if abandoned)
- ✅ Boxes clamp to image bounds; minimum size enforced
- ✅ `SubmitPreviewView` fully replaced (no dead code)
- ✅ `_repost_prompt` called after game post (sticky prompt moves down)
- ✅ Double-post lock preserved

**Placeholder scan:** None found.

**Type consistency:**
- `move_crop_box` / `zoom_crop_box` signatures match usage in `CropEditorView._on_*`
- `render_crop_editor` signature matches calls in `_rerender` and `veil_submit`
- `BoundingBox` used consistently throughout
