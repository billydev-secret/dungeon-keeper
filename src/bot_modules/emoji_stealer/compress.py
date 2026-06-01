"""GIF compression to fit Discord's 256 KB custom emoji size limit.

Pure-function — takes raw bytes, returns raw bytes. The cog imports
``compress_gif_for_emoji`` and applies it before upload.
"""

from __future__ import annotations

import io

from PIL import Image

from bot_modules.emoji_stealer.logic import DISCORD_MAX_EMOJI_BYTES


def compress_gif_for_emoji(data: bytes) -> bytes:
    """Resize animated GIF frames to fit under Discord's 256 KB emoji limit.

    Returns the original bytes unchanged if:
      - the input is already small enough, OR
      - the input isn't a GIF (the magic bytes don't match), OR
      - no resize between 96 → 32 px brings it under the limit.

    Otherwise returns a re-encoded GIF at the largest square size that fits.
    """
    if len(data) <= DISCORD_MAX_EMOJI_BYTES or not data[:3] == b"GIF":
        return data
    for dim in (96, 64, 48, 32):
        img = Image.open(io.BytesIO(data))
        frames: list[Image.Image] = []
        durations: list[int] = []
        loop = img.info.get("loop", 0)
        try:
            while True:
                frames.append(
                    img.convert("RGBA").resize((dim, dim), Image.Resampling.LANCZOS)
                )
                durations.append(img.info.get("duration", 100))
                img.seek(img.tell() + 1)
        except EOFError:
            pass
        if not frames:
            return data
        out = io.BytesIO()
        frames[0].save(
            out,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            loop=loop,
            duration=durations,
            optimize=True,
        )
        result = out.getvalue()
        if len(result) <= DISCORD_MAX_EMOJI_BYTES:
            return result
    return data
