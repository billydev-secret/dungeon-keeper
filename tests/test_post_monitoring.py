"""Spoiler enforcement — now content-aware.

The gate historically deleted *any* unspoilered image in a spoiler-required
channel, so a meme or a screenshot went the same way as explicit content.
These tests pin the narrowing, and pin the fallback direction: an image the
classifier could not read is still deleted, because unreadable must be treated
as maybe-explicit rather than opening a hole in the rule.
"""
from __future__ import annotations

import logging

import pytest

from bot_modules.core.post_monitoring import enforce_spoiler_requirement

log = logging.getLogger("test")

SPOILER_CHANNEL = 100
OTHER_CHANNEL = 200
BYPASS_ROLE = 900


class FakeRole:
    def __init__(self, role_id: int) -> None:
        self.id = role_id


class FakeAttachment:
    def __init__(self, filename: str = "pic.png", spoiler: bool = False) -> None:
        self.filename = filename
        self.id = abs(hash(filename)) % 10_000
        self._spoiler = spoiler

    def is_spoiler(self) -> bool:
        return self._spoiler


class FakeChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.name = "chan"
        self.sent: list[str] = []

    async def send(self, content: str, **_kwargs) -> None:
        self.sent.append(content)


class FakeMember:
    """A discord.Member for isinstance purposes — see the patch below."""

    def __init__(self, roles: list[FakeRole] | None = None) -> None:
        self.id = 42
        self.roles = roles or []

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

    async def delete(self) -> None:
        self.deleted = True


@pytest.fixture(autouse=True)
def _member_isinstance(monkeypatch):
    """Let FakeMember satisfy the isinstance(discord.Member) guard."""
    monkeypatch.setattr(
        "bot_modules.core.post_monitoring.discord.Member", FakeMember
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
        return value

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
        return attachment.filename == "nsfw.png"

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
