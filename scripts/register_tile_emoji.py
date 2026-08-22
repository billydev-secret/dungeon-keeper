"""Register the 37 Meadow Mahjong tile emoji as application-owned emoji.

The one-command prod step (spec §7.2; plan stage 5): application emoji are
uploaded to the bot *application*, usable in any guild, and cost no guild
slots. Requires live bot credentials, so this runs on the prod box, never in
a worktree:

    python scripts/register_tile_emoji.py            # upload + write id map
    python scripts/register_tile_emoji.py --dry-run  # list what would happen

Reads PNGs from assets/tile_emoji/ (generate with scripts/make_tile_emoji.py),
skips names already registered (safe to re-run; a reskin needs --replace),
and writes the id map to src/bot_modules/games/mahjong/tile_emoji.json —
the file tile_render.py resolves emoji through. Until this has run, the
text-chip fallback is the launch state and everything still renders.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import discord

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot_modules.core.config import load_config  # noqa: E402
from bot_modules.games.mahjong.tile_render import EMOJI_MAP_PATH  # noqa: E402
from bot_modules.games.mahjong.tiles import Tile  # noqa: E402

ASSET_DIR = PROJECT_ROOT / "assets" / "tile_emoji"
NAMES = [f"mm_{t.code}" for t in Tile] + ["mm_back"]


async def run(*, dry_run: bool, replace: bool) -> int:
    missing = [n for n in NAMES if not (ASSET_DIR / f"{n}.png").exists()]
    if missing:
        print(f"missing art for {missing} — run scripts/make_tile_emoji.py first")
        return 1

    config = load_config()
    client = discord.Client(intents=discord.Intents.none())
    await client.login(config.token)
    try:
        existing = {e.name: e for e in await client.fetch_application_emojis()}
        print(f"application already owns {len(existing)} emoji")
        id_map: dict[str, int] = {}
        for name in NAMES:
            code = name.removeprefix("mm_")
            if name in existing and not replace:
                id_map[code] = existing[name].id
                print(f"  = {name} (kept, id {existing[name].id})")
                continue
            if dry_run:
                print(f"  + {name} (would {'replace' if name in existing else 'upload'})")
                continue
            if name in existing:  # --replace: delete then re-upload
                await existing[name].delete()
            image = (ASSET_DIR / f"{name}.png").read_bytes()
            emoji = await client.create_application_emoji(name=name, image=image)
            id_map[code] = emoji.id
            print(f"  + {name} → id {emoji.id}")
        if not dry_run:
            EMOJI_MAP_PATH.write_text(
                json.dumps(id_map, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(f"id map written: {EMOJI_MAP_PATH} ({len(id_map)} entries)")
            print("Restart the bot to pick it up (tile_render caches at first use).")
    finally:
        await client.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--replace", action="store_true",
                    help="delete and re-upload names that already exist (reskin)")
    args = ap.parse_args()
    return asyncio.run(run(dry_run=args.dry_run, replace=args.replace))


if __name__ == "__main__":
    raise SystemExit(main())
