from __future__ import annotations

import io
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import discord

from bot_modules.core.utils import format_user_for_log
from bot_modules.services.nsfw_classifier_service import (
    SPOILER_IMAGE_EXTENSIONS,
    SURFACE_SFW,
    SURFACE_SPOILER,
    Classification,
    SfwPolicy,
    is_age_gated_channel,
    is_image_attachment,
)


def attachment_is_image(attachment: discord.Attachment) -> bool:
    """Delegates so this and the classifier can't disagree about what an
    image is — they previously differed over ``.tiff``, which made a .tiff
    upload reach the classifier and come back permanently UNKNOWN.

    :func:`enforce_spoiler_requirement` deliberately does *not* use this — see
    ``SPOILER_IMAGE_EXTENSIONS``."""
    return is_image_attachment(attachment)


def message_has_qualifying_image(message: discord.Message) -> bool:
    return any(attachment_is_image(attachment) for attachment in message.attachments)


@dataclass
class BlockedImage:
    """One image a gate destroyed, for the mod log and the blocked-images report.

    ``score`` is Marqo's whole-image probability — the number the verdict was
    actually made from, and the only one available in a channel that isn't
    age-gated. ``None`` means the image could not be read at all.

    ``label`` is a NudeNet tag and is populated **only** in age-gated channels,
    where the tagger runs. Anything rendering this must treat it as optional
    rather than as the headline.
    """

    message: discord.Message
    attachment: discord.Attachment
    surface: str
    score: float | None
    label: str | None
    deleted: bool


async def enforce_spoiler_requirement(
    message: discord.Message,
    *,
    spoiler_required_channels: frozenset[int] | set[int],
    bypass_role_ids: frozenset[int] | set[int],
    log: logging.Logger,
    classify: Callable[[discord.Attachment], Awaitable[Classification]] | None = None,
    report: Callable[[BlockedImage], Awaitable[None]] | None = None,
) -> bool:
    """Delete unspoilered images in spoiler-required channels.

    With *classify* supplied, an image is deleted when
    :attr:`Classification.requires_spoiler` says so — explicit by score, or a
    bare chest of any gender, or unreadable. A meme, a screenshot or a cat
    photo posted unspoilered is left alone, which is the false-positive class
    this gate historically produced.

    The bare-chest arm is a policy rule the model cannot express; it lives on
    the ``Classification`` rather than here so it is testable without a Discord
    message. See :meth:`Classification.requires_spoiler`.

    Without *classify* the original behavior applies unchanged — every
    unspoilered image goes. That is the correct fallback when no classifier is
    configured, and it keeps this function usable on its own.

    **A classifier that raises is treated as a classifier that said
    maybe-explicit.** The gate deletes, matching its own UNKNOWN fallback. A
    DB hiccup while reading the threshold used to propagate out of here and
    abort ``on_message`` entirely, which left the unspoilered image standing —
    a safety gate that opened on an exception.
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
        # Not attachment_is_image: this gate is narrower on purpose, because
        # everything it matches becomes deletable. See SPOILER_IMAGE_EXTENSIONS.
        if not attachment.filename.lower().endswith(SPOILER_IMAGE_EXTENSIONS):
            continue
        if attachment.is_spoiler():
            continue

        result: Classification | None = None
        if classify is not None:
            try:
                result = await classify(attachment)
            except Exception:
                # Fail closed. `result` stays None, which reaches the same
                # deletion path as "no classifier configured" — the strict
                # fallback, not the permissive one.
                log.exception(
                    "nsfw: classifier failed for attachment %s — deleting on the "
                    "safe side",
                    attachment.id,
                )

        if result is not None and not result.requires_spoiler:
            # Read it, and the rule doesn't apply — this is the deletion the
            # classifier exists to prevent. Keep checking the other
            # attachments; one innocent image doesn't clear the message.
            continue

        deleted = False
        try:
            log.info(
                "Deleting spoilerless image from %s: %s",
                message.author,
                message.content,
            )
            await message.delete()
            deleted = True
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

        if deleted and report is not None:
            await _report_safely(
                report,
                BlockedImage(
                    message=message,
                    attachment=attachment,
                    surface=SURFACE_SPOILER,
                    # None when the image was unreadable — this gate deletes on
                    # UNKNOWN by design, and that is the case most worth being
                    # able to find again.
                    score=result.score if result is not None else None,
                    label=result.top_label if result is not None else None,
                    deleted=True,
                ),
                log=log,
            )
        return True

    return False


async def _report_safely(
    report: Callable[[BlockedImage], Awaitable[None]],
    blocked: BlockedImage,
    *,
    log: logging.Logger,
) -> None:
    """Run a block report without letting it change the outcome.

    The audit trail failing must not alter what happened to the member, nor
    abort the rest of the on_message pipeline.
    """
    try:
        await report(blocked)
    except Exception:
        log.exception("nsfw: failed to report blocked image")


SFW_NOTICE = (
    "Beep Boop - friendly bot helper: that image looked explicit, so it was "
    "removed from this channel. If that was a mistake, let a mod know — a copy "
    "has been sent to you."
)


async def enforce_sfw_image_policy(
    message: discord.Message,
    *,
    policy: SfwPolicy,
    bypass_role_ids: frozenset[int] | set[int],
    log: logging.Logger,
    classify: Callable[[discord.Attachment], Awaitable[Classification]],
    report: Callable[[BlockedImage], Awaitable[None]] | None = None,
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

        # The score, not a label: this runs where the channel isn't age-gated,
        # so the tagger never ran and top_label is always None here.
        log.info(
            "nsfw: explicit image by %s in #%s (%.2f) — mode=%s",
            format_user_for_log(message.author),
            getattr(message.channel, "name", message.channel.id),
            result.score or 0.0,
            policy.mode,
        )

        deleted = False
        if policy.deletes:
            deleted = await _remove_and_return(message, attachment, log=log)

        if report is not None:
            await _report_safely(
                report,
                BlockedImage(
                    message=message,
                    attachment=attachment,
                    surface=SURFACE_SFW,
                    score=result.score,
                    label=result.top_label,
                    deleted=deleted,
                ),
                log=log,
            )

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
