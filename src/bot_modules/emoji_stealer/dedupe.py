"""Duplicate-detection helpers for the emoji stealer.

Two independent tiers, both pure and testable without Discord:

  - ``sha256_hex`` — exact byte match. Zero false positives: identical bytes
    are unquestionably the same image. Fires when a source CDN emoji is
    re-stolen into a guild that already holds a byte-identical copy.
  - ``perceptual_hash`` (colour dHash) + ``hamming`` — fuzzy "looks the same"
    match that survives Discord's re-encoding, our GIF compression, and
    resizing, yet still distinguishes same-shape-different-colour emoji.

Needs Pillow (already a dependency via ``compress.py``).

Design notes for the perceptual hash — both learned the hard way by measuring
distances on realistic transparent emoji:

  - **Keep colour.** A grayscale dHash collapses a red heart and a pink heart
    (and two differently-coloured squares) to the *same* silhouette, so it
    reports hamming 0 for plainly-different emoji. We hash each RGB channel
    separately instead, tripling the bit width to 192.
  - **Composite the alpha away.** Custom emoji are a small subject on a
    transparent field; ``convert`` alone composites transparency onto black,
    so every emoji shares a huge uniform background that dominates the hash.
    We flatten onto neutral grey first so the subject, not the background,
    drives the bits.

On realistic emoji this gives clean separation: a re-encoded/resized copy of
the same emoji stays well under ``DUPE_THRESHOLD`` while distinct emoji land far
above it. The cog still only *warns* on a hit, so the odd false positive costs
one extra click, never a refused steal.
"""

from __future__ import annotations

import hashlib
import io

from PIL import Image

# Per-channel dHash grid: an N×(N+1) thumbnail yields N×N horizontal
# comparisons per channel, so the hash is 3·N·N = 192 bits at N=8.
_HASH_GRID = 8
# Neutral grey the transparent emoji is flattened onto before hashing.
_HASH_BG = (128, 128, 128, 255)

# Hamming distance at or below this counts as "very similar". Measured on
# realistic transparent emoji, same-image re-encodes sit near ~12 and distinct
# emoji above ~38 (out of 192 bits); 20 splits them with margin on both sides.
# A named knob so it can be retuned without hunting through the cog.
DUPE_THRESHOLD = 20


def sha256_hex(data: bytes) -> str:
    """Hex SHA-256 of the raw bytes — the exact-match tier."""
    return hashlib.sha256(data).hexdigest()


def perceptual_hash(data: bytes) -> int | None:
    """192-bit colour difference hash of an image, or None if it won't decode.

    Flattens the (first, for animated) frame onto neutral grey to defuse the
    transparent background, resizes to ``(_HASH_GRID+1)×_HASH_GRID``, and emits
    one bit per horizontally-adjacent pixel pair (left brighter → 1) for each of
    the R, G and B channels. Re-encoding/resizing barely moves the hash; a
    different image — or the same shape in a different colour — moves it a lot.
    """
    try:
        img = Image.open(io.BytesIO(data))
        img.seek(0)  # first frame of an animated GIF
        rgba = img.convert("RGBA")
        flat = Image.alpha_composite(Image.new("RGBA", rgba.size, _HASH_BG), rgba)
        small = flat.convert("RGB").resize(
            (_HASH_GRID + 1, _HASH_GRID), Image.Resampling.LANCZOS
        )
    except Exception:
        return None
    raw = small.tobytes()  # RGB interleaved, row-major, (_HASH_GRID+1) wide
    stride = (_HASH_GRID + 1) * 3
    bits = 0
    for ch in range(3):
        for row in range(_HASH_GRID):
            base = row * stride + ch
            for col in range(_HASH_GRID):
                left = raw[base + col * 3]
                right = raw[base + (col + 1) * 3]
                bits = (bits << 1) | (1 if left > right else 0)
    return bits


def hamming(a: int, b: int) -> int:
    """Number of differing bits between two hashes."""
    return (a ^ b).bit_count()
