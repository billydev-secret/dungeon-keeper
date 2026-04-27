"""Shared text helpers used by command modules.

This module previously held an unregistered `/report` command tree
(`register_reports`) that was migrated into `cogs/reports_cog.py`. The cog
now owns the live commands; only the chunking/ephemeral helpers remain.
"""

from __future__ import annotations

import discord

SAFE_TEXT_CHUNK = 1900


def chunk_text(text: str, limit: int = SAFE_TEXT_CHUNK) -> list[str]:
    if not text:
        return [""]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")

    return chunks


async def send_ephemeral_text(interaction: discord.Interaction, text: str) -> None:
    for chunk in chunk_text(text):
        await interaction.followup.send(chunk, ephemeral=True)
