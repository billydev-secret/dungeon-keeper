"""Spoiler enforcement — now content-aware.

The gate historically deleted *any* unspoilered image in a spoiler-required
channel, so a meme or a screenshot went the same way as explicit content.
These tests pin the narrowing, and pin the fallback direction: an image the
classifier could not read is still deleted, because unreadable must be treated
as maybe-explicit rather than opening a hole in the rule.
"""
from __future__ import annotations

import logging

import discord
import pytest

from bot_modules.core.post_monitoring import (
    enforce_sfw_image_policy,
    enforce_spoiler_requirement,
)
from bot_modules.services.nsfw_classifier_service import (
    SFW_MODE_ENFORCE,
    SFW_MODE_LOG,
    SFW_MODE_OFF,
    Classification,
    SfwPolicy,
)

log = logging.getLogger("test")

SPOILER_CHANNEL = 100
OTHER_CHANNEL = 200
BYPASS_ROLE = 900
LOG_CHANNEL = 300


class FakeRole:
    def __init__(self, role_id: int) -> None:
        self.id = role_id


class _FakeResponse:
    """Minimal stand-in for the aiohttp response discord.Forbidden wants."""

    status = 403
    reason = "Forbidden"


class FakeAttachment:
    def __init__(self, filename: str = "pic.png", spoiler: bool = False) -> None:
        self.filename = filename
        self.id = abs(hash(filename)) % 10_000
        self.size = 1024
        # Mirror Discord: the content type follows the file, so a .txt upload
        # doesn't claim to be an image.
        self.content_type = (
            "image/png"
            if filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
            else "text/plain"
        )
        self._spoiler = spoiler
        self.owner: FakeMessage | None = None

    def is_spoiler(self) -> bool:
        return self._spoiler

    async def read(self) -> bytes:
        if self.owner is not None:
            self.owner.order.append("read")
        return b"imagebytes"


class FakeChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.name = "chan"
        self.sent: list[str] = []
        self.nsfw = False

    def is_nsfw(self) -> bool:
        return self.nsfw

    async def send(self, content: str, **_kwargs) -> None:
        self.sent.append(content)


class FakeMember:
    """A discord.Member for isinstance purposes — see the patch below."""

    def __init__(
        self, roles: list[FakeRole] | None = None, bot: bool = False
    ) -> None:
        self.id = 42
        self.roles = roles or []
        self.bot = bot
        self.dms: list[str] = []
        self.dm_error: Exception | None = None

    async def send(self, content: str, **_kwargs) -> None:
        if self.dm_error is not None:
            raise self.dm_error
        self.dms.append(content)

    def __str__(self) -> str:
        return "member#0001"


class FakeMessage:
    def __init__(
        self,
        *,
        attachments: list[FakeAttachment],
        channel_id: int = SPOILER_CHANNEL,
        author: object | None = None,
    ) -> None:
        self.attachments = attachments
        self.channel = FakeChannel(channel_id)
        self.author = author if author is not None else FakeMember()
        self.content = "look at this"
        self.deleted = False
        self.webhook_id: int | None = None
        self.order: list[str] = []
        for attachment in attachments:
            attachment.owner = self

    async def delete(self) -> None:
        self.order.append("delete")
        self.deleted = True


@pytest.fixture(autouse=True)
def _member_isinstance(monkeypatch):
    """Let FakeMember satisfy the isinstance(discord.Member) guard."""
    monkeypatch.setattr(
        "bot_modules.core.post_monitoring.discord.Member", FakeMember
    )


def classification(verdict_value, label="FEMALE_BREAST_EXPOSED", score=0.9):
    return Classification(
        attachment_id=1, verdict=verdict_value, top_label=label, top_score=score
    )


async def run(message, classify=None, bypass=frozenset()):
    return await enforce_spoiler_requirement(
        message,
        spoiler_required_channels={SPOILER_CHANNEL},
        bypass_role_ids=bypass,
        log=log,
        classify=classify,
    )


def verdict(value):
    async def classify(_attachment):
        return classification(value)

    return classify


# --------------------------------------------------------------------------
# the narrowing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_unspoilered_image_is_deleted():
    message = FakeMessage(attachments=[FakeAttachment()])

    assert await run(message, verdict(True)) is True
    assert message.deleted is True
    assert message.channel.sent  # the member is told why


@pytest.mark.asyncio
async def test_non_explicit_unspoilered_image_is_left_alone():
    # The whole point of stage 2: memes, screenshots and cat photos stop being
    # deleted for want of a spoiler tag.
    message = FakeMessage(attachments=[FakeAttachment()])

    assert await run(message, verdict(False)) is False
    assert message.deleted is False
    assert message.channel.sent == []


@pytest.mark.asyncio
async def test_unreadable_image_is_still_deleted():
    # UNKNOWN falls back to the pre-classifier behavior — a CDN failure must
    # not become a way to post explicit content unspoilered.
    message = FakeMessage(attachments=[FakeAttachment()])

    assert await run(message, verdict(None)) is True
    assert message.deleted is True


@pytest.mark.asyncio
async def test_without_a_classifier_every_unspoilered_image_goes():
    # The no-classifier fallback is the original rule, unchanged.
    message = FakeMessage(attachments=[FakeAttachment()])

    assert await run(message, None) is True
    assert message.deleted is True


@pytest.mark.asyncio
async def test_spoilered_explicit_image_is_never_classified_or_deleted():
    calls: list[object] = []

    async def classify(attachment):
        calls.append(attachment)
        return True

    message = FakeMessage(attachments=[FakeAttachment(spoiler=True)])

    assert await run(message, classify) is False
    assert message.deleted is False
    assert calls == []  # a spoilered image needs no verdict


@pytest.mark.asyncio
async def test_one_innocent_attachment_does_not_clear_an_explicit_one():
    # Mixed uploads: the innocent image must not short-circuit the check.
    innocent = FakeAttachment("cat.png")
    explicit = FakeAttachment("nsfw.png")
    message = FakeMessage(attachments=[innocent, explicit])

    async def classify(attachment):
        return classification(attachment.filename == "nsfw.png")

    assert await run(message, classify) is True
    assert message.deleted is True


# --------------------------------------------------------------------------
# guards that must survive the change
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_other_channels_are_untouched():
    message = FakeMessage(attachments=[FakeAttachment()], channel_id=OTHER_CHANNEL)

    assert await run(message, verdict(True)) is False
    assert message.deleted is False


@pytest.mark.asyncio
async def test_bypass_role_still_bypasses():
    message = FakeMessage(
        attachments=[FakeAttachment()],
        author=FakeMember(roles=[FakeRole(BYPASS_ROLE)]),
    )

    assert await run(message, verdict(True), bypass={BYPASS_ROLE}) is False
    assert message.deleted is False


@pytest.mark.asyncio
async def test_non_member_author_is_ignored():
    message = FakeMessage(attachments=[FakeAttachment()], author=object())

    assert await run(message, verdict(True)) is False
    assert message.deleted is False


@pytest.mark.asyncio
async def test_message_without_attachments_is_ignored():
    message = FakeMessage(attachments=[])

    assert await run(message, verdict(True)) is False


@pytest.mark.asyncio
async def test_non_image_attachment_is_ignored():
    calls: list[object] = []

    async def classify(attachment):
        calls.append(attachment)
        return True

    message = FakeMessage(attachments=[FakeAttachment("notes.txt")])

    assert await run(message, classify) is False
    assert message.deleted is False
    assert calls == []


# ==========================================================================
# SFW nudity prevention — the only check here that destroys user content
# ==========================================================================


ENFORCING = SfwPolicy(mode=SFW_MODE_ENFORCE, log_channel_id=LOG_CHANNEL)


def sfw_verdict(value):
    async def classify(_attachment):
        return classification(value)

    return classify


async def run_sfw(message, classify=None, policy=ENFORCING, bypass=frozenset()):
    reports: list = []

    async def report(violation):
        reports.append(violation)

    deleted = await enforce_sfw_image_policy(
        message,
        policy=policy,
        bypass_role_ids=bypass,
        log=log,
        classify=classify if classify is not None else sfw_verdict(True),
        report=report,
    )
    return deleted, reports


@pytest.mark.asyncio
async def test_sfw_explicit_image_is_removed_and_reported():
    message = FakeMessage(attachments=[FakeAttachment()], channel_id=OTHER_CHANNEL)

    deleted, reports = await run_sfw(message)

    assert deleted is True
    assert message.deleted is True
    assert message.channel.sent  # public notice
    assert message.author.dms  # image returned to the poster
    assert len(reports) == 1
    assert reports[0].deleted is True
    assert reports[0].label == "FEMALE_BREAST_EXPOSED"


@pytest.mark.asyncio
async def test_sfw_fails_open_on_an_unreadable_image():
    # Opposite of spoiler enforcement: acting on a failed read here would
    # delete an innocent member's photo.
    message = FakeMessage(attachments=[FakeAttachment()], channel_id=OTHER_CHANNEL)

    deleted, reports = await run_sfw(message, sfw_verdict(None))

    assert deleted is False
    assert message.deleted is False
    assert reports == []


@pytest.mark.asyncio
async def test_sfw_leaves_non_explicit_images_alone():
    message = FakeMessage(attachments=[FakeAttachment()], channel_id=OTHER_CHANNEL)

    deleted, _ = await run_sfw(message, sfw_verdict(False))

    assert deleted is False
    assert message.deleted is False


@pytest.mark.asyncio
async def test_sfw_bot_uploads_are_exempt():
    # Regression for the Guess collision: the bot posts SPOILER_guess_full.jpg
    # and friends. Without this exemption, shipping stage 3 would delete the
    # bot's own game content in any Guess channel not marked NSFW.
    message = FakeMessage(
        attachments=[FakeAttachment("SPOILER_guess_full.jpg")],
        channel_id=OTHER_CHANNEL,
        author=FakeMember(bot=True),
    )

    deleted, _ = await run_sfw(message)

    assert deleted is False
    assert message.deleted is False


@pytest.mark.asyncio
async def test_sfw_webhook_uploads_are_exempt():
    message = FakeMessage(attachments=[FakeAttachment()], channel_id=OTHER_CHANNEL)
    message.webhook_id = 12345

    deleted, _ = await run_sfw(message)

    assert deleted is False
    assert message.deleted is False


@pytest.mark.asyncio
async def test_sfw_age_gated_channels_are_exempt():
    # Explicit content belongs in age-gated channels; this check is for the
    # spaces that aren't.
    message = FakeMessage(attachments=[FakeAttachment()], channel_id=OTHER_CHANNEL)
    message.channel.nsfw = True

    deleted, _ = await run_sfw(message)

    assert deleted is False
    assert message.deleted is False


@pytest.mark.asyncio
async def test_sfw_exempt_channels_are_skipped():
    message = FakeMessage(attachments=[FakeAttachment()], channel_id=OTHER_CHANNEL)
    policy = SfwPolicy(mode=SFW_MODE_ENFORCE, exempt_channel_ids=frozenset({OTHER_CHANNEL}))

    deleted, _ = await run_sfw(message, policy=policy)

    assert deleted is False
    assert message.deleted is False


@pytest.mark.asyncio
async def test_sfw_bypass_role_is_exempt():
    message = FakeMessage(
        attachments=[FakeAttachment()],
        channel_id=OTHER_CHANNEL,
        author=FakeMember(roles=[FakeRole(BYPASS_ROLE)]),
    )

    deleted, _ = await run_sfw(message, bypass={BYPASS_ROLE})

    assert deleted is False
    assert message.deleted is False


@pytest.mark.asyncio
async def test_sfw_off_mode_does_not_even_classify():
    calls: list = []

    async def classify(attachment):
        calls.append(attachment)
        return classification(True)

    message = FakeMessage(attachments=[FakeAttachment()], channel_id=OTHER_CHANNEL)

    deleted, _ = await run_sfw(
        message, classify, policy=SfwPolicy(mode=SFW_MODE_OFF)
    )

    assert deleted is False
    assert calls == []  # ships inert: no downloads, no inference, no cost


@pytest.mark.asyncio
async def test_sfw_log_mode_reports_without_deleting():
    # The shakedown mode: measure real accuracy before trusting it to delete.
    message = FakeMessage(attachments=[FakeAttachment()], channel_id=OTHER_CHANNEL)

    deleted, reports = await run_sfw(
        message, policy=SfwPolicy(mode=SFW_MODE_LOG, log_channel_id=LOG_CHANNEL)
    )

    assert deleted is False
    assert message.deleted is False
    assert message.author.dms == []
    assert len(reports) == 1
    assert reports[0].deleted is False


@pytest.mark.asyncio
async def test_sfw_report_failure_does_not_change_the_outcome():
    # The audit trail failing must not abort the on_message pipeline or
    # resurrect the message.
    message = FakeMessage(attachments=[FakeAttachment()], channel_id=OTHER_CHANNEL)

    async def failing_report(_violation):
        raise RuntimeError("mod log channel is gone")

    deleted = await enforce_sfw_image_policy(
        message,
        policy=ENFORCING,
        bypass_role_ids=frozenset(),
        log=log,
        classify=sfw_verdict(True),
        report=failing_report,
    )

    assert deleted is True
    assert message.deleted is True


@pytest.mark.asyncio
async def test_sfw_returns_the_image_before_deleting():
    # A wrong call should cost the member their post, not their file — so the
    # read has to happen while the attachment is still guaranteed fetchable.
    message = FakeMessage(attachments=[FakeAttachment()], channel_id=OTHER_CHANNEL)

    await run_sfw(message)

    assert message.author.dms
    assert message.order.index("read") < message.order.index("delete")


@pytest.mark.asyncio
async def test_sfw_closed_dms_do_not_block_removal():
    message = FakeMessage(attachments=[FakeAttachment()], channel_id=OTHER_CHANNEL)
    message.author.dm_error = discord.Forbidden(_FakeResponse(), "closed")

    deleted, _ = await run_sfw(message)

    assert deleted is True
    assert message.deleted is True


@pytest.mark.asyncio
async def test_sfw_non_image_attachments_are_ignored():
    calls: list = []

    async def classify(attachment):
        calls.append(attachment)
        return classification(True)

    message = FakeMessage(
        attachments=[FakeAttachment("notes.txt")], channel_id=OTHER_CHANNEL
    )

    deleted, _ = await run_sfw(message, classify)

    assert deleted is False
    assert calls == []
