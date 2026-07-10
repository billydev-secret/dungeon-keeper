"""Tests for the emoji-stealer duplicate-detection helpers.

Covers ``bot_modules/emoji_stealer/dedupe.py`` — the exact (SHA-256) and
perceptual (dHash) tiers used to warn before adding an emoji a guild already
has. Pure functions, no Discord glue.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from bot_modules.emoji_stealer.dedupe import (
    DUPE_THRESHOLD,
    hamming,
    perceptual_hash,
    sha256_hex,
)


def _png(color: tuple[int, int, int], size: int = 64) -> bytes:
    img = Image.new("RGB", (size, size), color=color)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _emoji(shape: str, color: tuple[int, int, int, int], size: int = 128) -> bytes:
    """A small coloured shape on a TRANSPARENT background — what a real custom
    emoji actually is, and the input that broke a naive grayscale dHash."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = size // 4
    if shape == "circle":
        d.ellipse([m, m, size - m, size - m], fill=color)
    elif shape == "square":
        d.rectangle([m, m, size - m, size - m], fill=color)
    elif shape == "triangle":
        d.polygon([(size // 2, m), (m, size - m), (size - m, size - m)], fill=color)
    else:
        raise ValueError(shape)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _reencode(data: bytes, size: int) -> bytes:
    """Resize + re-save, preserving alpha — mimics how Discord serves back a
    stolen copy (still a transparent PNG, possibly rescaled/recompressed)."""
    img = Image.open(io.BytesIO(data)).convert("RGBA").resize(
        (size, size), Image.Resampling.LANCZOS
    )
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


# ── sha256_hex ───────────────────────────────────────────────────────


def test_sha256_hex_identical_bytes_match():
    data = _png((255, 0, 0))
    assert sha256_hex(data) == sha256_hex(data)


def test_sha256_hex_different_bytes_differ():
    assert sha256_hex(_png((255, 0, 0))) != sha256_hex(_png((0, 255, 0)))


def test_sha256_hex_is_hex_of_expected_length():
    h = sha256_hex(b"anything")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


# ── hamming ──────────────────────────────────────────────────────────


def test_hamming_zero_for_equal():
    assert hamming(0b1010, 0b1010) == 0


def test_hamming_counts_differing_bits():
    assert hamming(0b0000, 0b1011) == 3


# ── perceptual_hash ──────────────────────────────────────────────────


def test_perceptual_hash_returns_none_for_non_image():
    assert perceptual_hash(b"<!DOCTYPE html> not an image") is None


def test_perceptual_hash_stable_for_same_image():
    data = _emoji("circle", (255, 0, 0, 255))
    assert perceptual_hash(data) == perceptual_hash(data)


def test_perceptual_hash_survives_reencode_and_resize():
    """The whole point: a re-encoded, resized copy of the same emoji lands
    within the duplicate threshold — i.e. a re-steal is recognised."""
    original = _emoji("circle", (255, 0, 0, 255))
    reencoded = _reencode(original, size=40)
    h1 = perceptual_hash(original)
    h2 = perceptual_hash(reencoded)
    assert h1 is not None and h2 is not None
    assert hamming(h1, h2) <= DUPE_THRESHOLD


def test_perceptual_hash_distinguishes_same_shape_different_colour():
    """The bug that killed the grayscale version: two identically-shaped emoji
    in different colours must NOT collide — colour has to survive into the
    hash, so they land outside the duplicate threshold."""
    red = perceptual_hash(_emoji("square", (255, 0, 0, 255)))
    blue = perceptual_hash(_emoji("square", (0, 90, 255, 255)))
    assert red is not None and blue is not None
    assert hamming(red, blue) > DUPE_THRESHOLD


def test_perceptual_hash_distinguishes_different_shapes():
    a = perceptual_hash(_emoji("circle", (0, 200, 0, 255)))
    b = perceptual_hash(_emoji("triangle", (0, 200, 0, 255)))
    assert a is not None and b is not None
    assert hamming(a, b) > DUPE_THRESHOLD


def test_perceptual_hash_reads_first_frame_of_animated_gif():
    """An animated GIF must hash (its first frame), not raise."""
    frames = [Image.new("RGBA", (64, 64), color=(c, c, c, 255)) for c in (30, 200)]
    out = io.BytesIO()
    frames[0].save(
        out, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0
    )
    assert perceptual_hash(out.getvalue()) is not None
