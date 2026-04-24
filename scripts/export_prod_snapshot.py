#!/usr/bin/env python3
"""Export prod guild structure to prod_snapshot.json (spec §4.3.3).

Run this against the prod bot (with DISCORD_TOKEN_PROD and GUILD_ID_PROD set)
whenever prod channel/role structure changes. The output is committed to the
repo so the dev bot can build its ID remap table on startup.

Usage:
    BOT_ENV=prod python scripts/export_prod_snapshot.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

import discord

_OUTPUT = _ROOT / "prod_snapshot.json"


async def main() -> None:
    import os

    token = os.environ.get("DISCORD_TOKEN_PROD")
    guild_id_raw = os.environ.get("GUILD_ID_PROD")

    if not token or not guild_id_raw:
        print("ERROR: DISCORD_TOKEN_PROD and GUILD_ID_PROD must be set.", file=sys.stderr)
        sys.exit(1)

    guild_id = int(guild_id_raw)

    intents = discord.Intents.default()
    intents.members = False
    client = discord.Client(intents=intents)

    snapshot: dict = {}

    @client.event
    async def on_ready() -> None:
        guild = client.get_guild(guild_id)
        if guild is None:
            print(f"ERROR: Bot is not in guild {guild_id}.", file=sys.stderr)
            await client.close()
            return

        categories = [
            {"id": cat.id, "name": cat.name}
            for cat in guild.categories
        ]
        channels = [
            {
                "id": ch.id,
                "name": ch.name,
                "type": "text",
                "parent_id": ch.category_id,
                "parent_name": ch.category.name if ch.category else None,
            }
            for ch in guild.text_channels
        ]
        roles = [
            {"id": r.id, "name": r.name, "position": r.position}
            for r in guild.roles
            if not r.is_default()
        ]

        snapshot.update(
            {
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "guild_id": guild.id,
                "guild_name": guild.name,
                "bot_user_id": client.user.id if client.user else 0,
                "categories": categories,
                "channels": channels,
                "roles": roles,
            }
        )

        _OUTPUT.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        print(f"Exported snapshot for {guild.name!r}")
        print(f"  {len(categories)} categories, {len(channels)} channels, {len(roles)} roles")
        print(f"  Written to {_OUTPUT}")
        await client.close()

    await client.start(token)


if __name__ == "__main__":
    asyncio.run(main())
