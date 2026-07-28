from __future__ import annotations

import io
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import discord

from bot_modules.core.utils import format_user_for_log
from bot_modules.services.nsfw_classifier_service import (
    Classification,
    SfwPolicy,
    is_age_gated_channel,
)


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


SFW_NOTICE = (
    "Beep Boop - friendly bot helper: that image looked explicit, so it was "
    "removed from this channel. If that was a mistake, let a mod know — a copy "
    "has been sent to you."
)


@dataclass
class SfwViolation:
    """One explicit image found in a SFW channel, for the mod-log record."""

    message: discord.Message
    attachment: discord.Attachment
    label: str | None
    score: float | None
    deleted: bool


async def enforce_sfw_image_policy(
    message: discord.Message,
    *,
    policy: SfwPolicy,
    bypass_role_ids: frozenset[int] | set[int],
    log: logging.Logger,
    classify: Callable[[discord.Attachment], Awaitable[Classification]],
    report: Callable[[SfwViolation], Awaitable[None]] | None = None,
) -> bool:
    """Remove explicit images posted in channels that aren't age-gated.

    Returns True only when the message was actually deleted.

    **Fails open.** An image the classifier could not read is left alone —
    the opposite of :func:`enforce_spoiler_requirement`, and deliberately so:
    there, a failed read risks explicit content staying up in a channel that
    expects spoilers; here, acting on a failed read would delete an innocent
    member's photo. The classification also runs at the stricter SFW
    threshold, because this is the only check in the module that destroys
    content.

    Bots and webhooks are exempt outright. The Guess game uploads explicit
    images itself (``SPOILER_guess_full.jpg`` and friends), so without this
    exemption the bot would delete its own game content in any Guess channel
    that isn't Discord-marked NSFW.
    """
    if not policy.is_active:
        return False

    if message.channel.id in policy.exempt_channel_ids:
        return False

    # Age-gated channels are where explicit content belongs; spoiler-required
    # channels have their own rule (a spoiler tag makes it acceptable there),
    # and enforce_spoiler_requirement owns them.
    if is_age_gated_channel(message.channel):
        return False

    if getattr(message.author, "bot", False) or message.webhook_id is not None:
        return False

    if not isinstance(message.author, discord.Member):
        return False

    if any(role.id in bypass_role_ids for role in message.author.roles):
        return False

    for attachment in message.attachments:
        if not attachment_is_image(attachment):
            continue

        result = await classify(attachment)
        if result.verdict is not True:
            # False (read it, not explicit) and None (couldn't read it) both
            # leave the image alone.
            continue

        log.info(
            "nsfw: explicit image by %s in #%s (%s %.2f) — mode=%s",
            format_user_for_log(message.author),
            getattr(message.channel, "name", message.channel.id),
            result.top_label,
            result.top_score or 0.0,
            policy.mode,
        )

        deleted = False
        if policy.deletes:
            deleted = await _remove_and_return(message, attachment, log=log)

        if report is not None:
            try:
                await report(
                    SfwViolation(
                        message=message,
                        attachment=attachment,
                        label=result.top_label,
                        score=result.top_score,
                        deleted=deleted,
                    )
                )
            except Exception:
                # The audit trail failing must not change the outcome for the
                # member, nor abort the rest of the on_message pipeline.
                log.exception("nsfw: failed to report SFW violation")

        return deleted

    return False


async def _remove_and_return(
    message: discord.Message,
    attachment: discord.Attachment,
    *,
    log: logging.Logger,
) -> bool:
    """Delete the message, DM the image back, and post a brief notice.

    The DM is attempted **before** the delete, while the attachment is
    guaranteed fetchable — a wrong call should cost the member their post, not
    their file.
    """
    payload: bytes | None = None
    try:
        payload = await attachment.read()
    except (discord.HTTPException, discord.NotFound) as e:
        log.warning("nsfw: could not re-read attachment for return DM: %s", e)

    try:
        await message.delete()
    except discord.Forbidden:
        log.warning(
            "nsfw: missing permission to delete explicit image in %s from %s",
            getattr(message.channel, "name", message.channel.id),
            format_user_for_log(message.author),
        )
        return False
    except discord.HTTPException as e:
        log.error("nsfw: failed to delete explicit image: %s", e)
        return False

    if payload is not None:
        try:
            await message.author.send(
                "Your image was removed from "
                f"#{getattr(message.channel, 'name', 'a channel')} — here it is back.",
                file=discord.File(
                    io.BytesIO(payload), filename=f"SPOILER_{attachment.filename}"
                ),
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            # Closed DMs are common and are not an error worth escalating.
            log.info("nsfw: could not DM the image back: %s", e)

    try:
        await message.channel.send(SFW_NOTICE, delete_after=8)
    except discord.HTTPException as e:
        log.warning("nsfw: could not post removal notice: %s", e)

    return True
