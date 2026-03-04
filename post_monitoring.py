from __future__ import annotations

import discord
import logging


def attachment_is_image(attachment: discord.Attachment) -> bool:
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    filename = attachment.filename.lower()
    return filename.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"))


def message_has_qualifying_image(message: discord.Message) -> bool:
    return any(attachment_is_image(attachment) for attachment in message.attachments)


async def enforce_spoiler_requirement(
    message: discord.Message,
    *,
    spoiler_required_channels: set[int],
    bypass_role_ids: set[int],
    log: logging.Logger,
) -> bool:
    if message.channel.id not in spoiler_required_channels:
        return False

    if not isinstance(message.author, discord.Member):
        return False

    if any(role.id in bypass_role_ids for role in message.author.roles):
        return False

    if not message.attachments:
        return False

    for attachment in message.attachments:
        filename = attachment.filename.lower()
        if not filename.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            continue
        if attachment.is_spoiler():
            continue

        try:
            log.info("Deleting spoilerless image from %s: %s", message.author, message.content)
            await message.delete()
            await message.channel.send(
                "Beep Boop - friendly bot helper: Images in this channel must be marked as spoiler.",
                delete_after=5,
            )
        except discord.Forbidden:
            pass
        return True

    return False
