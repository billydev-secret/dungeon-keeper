"""Shared image → representative-colour helpers.

Used by the branding accent resolver (and anything else that wants to
derive a colour from an avatar/logo). Kept dependency-light: Pillow is
imported lazily so importing this module never fails when Pillow is
absent — callers get ``None`` and substitute a default.
"""

from __future__ import annotations

import colorsys
from io import BytesIO
from typing import Optional

import discord

# Saturation floor below which a colour bucket is treated as gray when
# picking a vivid highlight (see ``dominant_highlight_color``).
_MIN_VIVID_SAT = 0.20


def dominant_highlight_color(image_bytes: bytes) -> Optional[discord.Colour]:
    """Extract a vivid "highlight" colour from an image.

    Opaque pixels are grouped into a coarse RGB grid and each bucket is
    scored by ``count * saturation**2`` so a saturated brand colour wins
    over a large but dull/gray background. When the image is essentially
    grayscale (no bucket clears ``_MIN_VIVID_SAT``) we fall back to the
    most common opaque colour so the result stays stable instead of
    latching onto an arbitrary edge pixel.

    Returns ``None`` when Pillow is unavailable, the image can't be
    decoded, or there are no opaque pixels — callers should substitute a
    sensible default (e.g. the guild's role colour).
    """
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        img = Image.open(BytesIO(image_bytes)).convert("RGBA")
    except Exception:
        return None
    img.thumbnail((64, 64))

    # bucket key -> [count, r_sum, g_sum, b_sum]
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for r, g, b, a in img.getdata():
        if a < 128:
            continue
        key = (r >> 4, g >> 4, b >> 4)
        acc = buckets.get(key)
        if acc is None:
            buckets[key] = [1, r, g, b]
        else:
            acc[0] += 1
            acc[1] += r
            acc[2] += g
            acc[3] += b
    if not buckets:
        return None

    best_vivid: Optional[tuple[int, int, int]] = None
    best_vivid_score = 0.0
    best_common: Optional[tuple[int, int, int]] = None
    best_common_count = -1
    for count, r_sum, g_sum, b_sum in buckets.values():
        r = r_sum // count
        g = g_sum // count
        b = b_sum // count
        sat = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)[1]
        score = count * (sat ** 2)
        if sat >= _MIN_VIVID_SAT and score > best_vivid_score:
            best_vivid_score = score
            best_vivid = (r, g, b)
        if count > best_common_count:
            best_common_count = count
            best_common = (r, g, b)

    chosen = best_vivid or best_common
    if chosen is None:
        return None
    return discord.Colour.from_rgb(*chosen)
