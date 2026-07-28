from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import discord

from bot_modules.core.utils import format_user_for_log


def attachment_is_image(attachment: discord.Attachment) -> bool:
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    filename = attachment.filename.lower()
    return filename.endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff")
    )


def message_has_qualifying_image(message: discord.Message) -> bool:
    return any(attachment_is_image(attachment) for attachment in message.attachments)


async def enforce_spoiler_requirement(
    message: discord.Message,
    *,
    spoiler_required_channels: frozenset[int] | set[int],
    bypass_role_ids: frozenset[int] | set[int],
    log: logging.Logger,
    classify: Callable[[discord.Attachment], Awaitable[bool | None]] | None = None,
) -> bool:
    """Delete unspoilered images in spoiler-required channels.

    With *classify* supplied, only images that classify as **explicit** are
    deleted — a meme, a screenshot or a cat photo posted unspoilered is left
    alone, which is the false-positive class this gate historically produced.
    An image the classifier could not read (``None``) is deleted anyway:
    unreadable is treated as maybe-explicit, so a CDN failure falls back to
    the pre-classifier behavior rather than opening a hole in the rule.

    Without *classify* the original behavior applies unchanged — every
    unspoilered image goes. That is the correct fallback when no classifier is
    configured, and it keeps this function usable on its own.
    """
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

        if classify is not None and await classify(attachment) is False:
            # Read it, and it isn't explicit — this is the deletion the
            # classifier exists to prevent. Keep checking the other
            # attachments; one innocent image doesn't clear the message.
            continue

        try:
            log.info(
                "Deleting spoilerless image from %s: %s",
                message.author,
                message.content,
            )
            await message.delete()
            await message.channel.send(
                "Beep Boop - friendly bot helper: Images in this channel must be marked as spoiler.",
                delete_after=5,
            )
        except discord.Forbidden:
            channel_name = getattr(message.channel, "name", None)
            channel_label = (
                f"#{channel_name} ({message.channel.id})"
                if channel_name
                else str(message.channel.id)
            )
            log.warning(
                "Missing permission to delete spoilerless image in channel %s from user %s",
                channel_label,
                format_user_for_log(message.author),
            )
        except discord.HTTPException as e:
            channel_name = getattr(message.channel, "name", None)
            channel_label = (
                f"#{channel_name} ({message.channel.id})"
                if channel_name
                else str(message.channel.id)
            )
            log.error(
                "Failed to enforce spoiler requirement in channel %s: %s",
                channel_label,
                e,
            )
        return True

    return False
