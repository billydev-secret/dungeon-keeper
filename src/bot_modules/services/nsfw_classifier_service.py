"""Shared NSFW image classifier — one verdict per attachment, three consumers.

Reaction tipping, spoiler enforcement and SFW nudity prevention all need to
know whether an uploaded image is explicit. They fire off the same
``on_message``, so classification happens **once** per attachment and the
result is shared (see :func:`classify_attachment`'s cache).

Two models, doing different jobs:

* **Marqo** (:mod:`marqo_nsfw`) produces the verdict, in every channel. It is
  the only thing any consumer acts on.
* **NudeNet** (:mod:`guess_nudenet`) produces labels and boxes, in age-gated
  channels **only**, purely to fill ``nsfw_detections``. It never changes what
  happens to an image.

That split is why tagging lives on the recording path rather than beside the
verdict: it makes it structurally impossible to derive a body-part inventory
of an upload in general chat, which is the thing the privacy rule forbids. It
also costs less than the arrangement it replaced, where NudeNet ran everywhere
and its labels were discarded outside age-gated channels.

The three consumers disagree about which way a *failure* should fall, so this
module never picks for them: an unreadable or unclassifiable image yields
``verdict=None`` (:data:`UNKNOWN`), and each caller applies its own fallback.
Tipping reacts anyway (a CDN hiccup must not cost a poster), spoiler
enforcement deletes (preserving today's behavior), SFW prevention does nothing
(never delete on a failed read).

A fourth caller, :func:`observe_images`, is not a consumer at all: it asks for
a verdict on every image in an age-gated channel so the metrics table stops
describing only the images a gate happened to judge, and then does nothing
with the answer. It is opt-in per guild.

Both models are imported lazily, so importing this module is free and safe on
machines without the weights. See docs/nsfw_classifier_spec.md.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from bot_modules.core.db_utils import (
    get_config_id_set,
    get_config_value,
    open_db,
    parse_bool,
)
from bot_modules.services.guess_models import Detection

log = logging.getLogger("dungeonkeeper.nsfw")

#: Labels NudeNet may report as the headline tag for a recorded row. Exposed
#: nudity only — the paired ``*_COVERED`` labels (lingerie, swimwear, implied)
#: deliberately do not qualify. ``SEX_ACT`` is synthesised by
#: guess_pipeline.merge_sex_act_detections when two different genital labels
#: overlap.
#:
#: Descriptive metadata, not a gate: Marqo decides the verdict on its own, so
#: no label here can make an image explicit or spare it.
DEFAULT_LABEL_SET: frozenset[str] = frozenset({
    "MALE_GENITALIA_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "SEX_ACT",
})

#: Probability at or above which an image counts as explicit. Consumers that
#: destroy content use a higher one — see CONFIG_KEY_SFW_THRESHOLD. Both sit in
#: a wide empty gap between the measured controls and the measured true
#: positive; docs/nsfw_classifier_spec.md carries the numbers.
DEFAULT_THRESHOLD = 0.5

#: SFW nudity prevention deletes a member's upload on a positive, so it demands
#: more certainty than merely qualifying a post for coins.
DEFAULT_SFW_THRESHOLD = 0.75

CONFIG_KEY_THRESHOLD = "nsfw_classifier_threshold"
CONFIG_KEY_SFW_THRESHOLD = "nsfw_classifier_sfw_threshold"
CONFIG_KEY_SFW_MODE = "nsfw_sfw_prevention_mode"
CONFIG_KEY_SFW_LOG_CHANNEL = "nsfw_sfw_prevention_log_channel_id"
CONFIG_BUCKET_SFW_EXEMPT = "nsfw_prevention_exempt_channels"

#: Classify every image in an age-gated channel, whether or not a gate needed
#: a verdict for it. Off by default — see :func:`observe_images` for why this
#: is a choice a server makes rather than something that just happens.
CONFIG_KEY_OBSERVE = "nsfw_observe_age_gated"

#: SFW nudity prevention is the only thing here that destroys a member's
#: upload, so it ships **off** and is turned on from the dashboard. ``log``
#: is the shakedown mode: it reports what it *would* have deleted, so real
#: accuracy can be measured against real traffic before anything is lost.
SFW_MODE_OFF = "off"
SFW_MODE_LOG = "log"
SFW_MODE_ENFORCE = "enforce"
SFW_MODES = (SFW_MODE_OFF, SFW_MODE_LOG, SFW_MODE_ENFORCE)
DEFAULT_SFW_MODE = SFW_MODE_OFF

#: Which gate destroyed an image, for the blocked-images report.
SURFACE_SFW = "sfw"
SURFACE_SPOILER = "spoiler"

#: What was actually done. ``logged`` is SFW prevention in ``log`` mode: the
#: image survived, and the row records what would have happened.
ACTION_REMOVED = "removed"
ACTION_LOGGED = "logged"

#: Images larger than this are not downloaded. Guards against a member pinning
#: the bot's bandwidth with a huge upload; Discord's own limit is well under
#: this for most users.
MAX_IMAGE_BYTES = 25 * 1024 * 1024

#: Seconds to wait for an attachment download before giving up as UNKNOWN.
DOWNLOAD_TIMEOUT_SECONDS = 10.0

#: Verdict for "we could not tell" — distinct from False ("we looked, it isn't
#: explicit"). Callers MUST branch on this rather than treating it as False.
UNKNOWN = None

#: attachment id -> ``(tagged, task)`` for in-flight or completed work, so
#: concurrent consumers share one download and one pass over the image.
#:
#: One entry covers both models. They could be cached separately, but each
#: would then need its own download of the same bytes, and holding the bytes to
#: avoid that would mean keeping up to 25 MB per cached attachment alive. So a
#: single task downloads once, scores, and tags in the same pass.
#:
#: ``tagged`` records whether that pass included NudeNet. It is a property of
#: the attachment's *channel*, and an attachment belongs to exactly one
#: message, so it cannot differ between two consumers of the same entry — the
#: mismatch branch in :func:`_shared_infer` exists to keep that assumption from
#: failing silently if it ever stops holding.
#:
#: What is held is the *score*, not a verdict, so an entry stays valid across
#: threshold edits and across consumers applying different bars.
_CACHE_MAX = 512
_cache: OrderedDict[
    int, "tuple[bool, asyncio.Task[tuple[float, list[Detection], int, int]]]"
] = OrderedDict()


class SupportsAttachment(Protocol):
    """The slice of ``discord.Attachment`` this service uses.

    Declared structurally so tests exercise the real code path with a stub
    instead of a Discord mock.
    """

    id: int
    filename: str
    size: int
    content_type: str | None

    async def read(self) -> bytes: ...


def model_name(*, tagged: bool) -> str:
    """Which weights produced a row, for ``nsfw_classifications.model``.

    Names both engines when NudeNet also ran, because a row that says only
    ``marqo-384`` cannot later be told apart from one whose tags are missing.
    The NudeNet half is read back from the loaded detector rather than assumed:
    which file wins depends on what is on disk.
    """
    from bot_modules.services.marqo_nsfw import MODEL_NAME  # noqa: PLC0415

    if not tagged:
        return MODEL_NAME
    from bot_modules.services.guess_nudenet import active_model_name  # noqa: PLC0415

    tagger = active_model_name()
    return f"{MODEL_NAME}+{tagger}" if tagger else MODEL_NAME


@dataclass
class Classification:
    """One attachment's verdict plus everything needed to interpret it later."""

    attachment_id: int
    verdict: bool | None
    #: Marqo's probability that the image is explicit — what the verdict was
    #: actually made from. ``None`` only when the verdict is UNKNOWN.
    score: float | None = None
    #: Highest-scoring qualifying NudeNet tag and its confidence. Both stay
    #: ``None`` outside age-gated channels, where NudeNet never runs — so a
    #: caller that wants a number to show a moderator wants :attr:`score`.
    top_label: str | None = None
    top_score: float | None = None
    detections: list[Detection] = field(default_factory=list)
    #: Whether the tagger ran, which is not the same as whether it found
    #: anything. An image it ran over and saw nothing in is the interesting
    #: case — the blind spot this engine swap exists to cover — so the two must
    #: stay distinguishable in the recorded row.
    tagged: bool = False
    inference_ms: int = 0
    size_bytes: int = 0
    threshold: float = DEFAULT_THRESHOLD

    @property
    def is_unknown(self) -> bool:
        return self.verdict is UNKNOWN


@dataclass(frozen=True)
class Block:
    """One image a gate destroyed, for the blocked-images report.

    ``score`` is ``None`` when the image could not be read at all — spoiler
    enforcement deletes on an unreadable image by design, and a row that
    claimed 0.0 would read as "the model was sure it was clean, and we deleted
    it anyway".
    """

    message_id: int
    attachment_id: int
    guild_id: int
    channel_id: int
    author_id: int
    filename: str
    score: float | None
    surface: str
    action: str


#: What classification treats as an image. ``post_monitoring.attachment_is_image``
#: delegates here and the auto-react cog filters with :func:`is_classifiable`,
#: so the copies that used to disagree (over ``.tiff``, and over attachments
#: Discord serves with no ``content_type``) can no longer drift apart.
#:
#: :data:`SPOILER_IMAGE_EXTENSIONS` is the documented exception — read that
#: before adding a format here, because it will not move with this one.
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff")

#: What spoiler enforcement treats as an image — deliberately a strict subset,
#: and deliberately without the ``content_type`` fallback that
#: :func:`is_image_attachment` applies.
#:
#: This gate deletes members' posts, including on an unreadable image, so
#: widening it is a behaviour change and not a tidy-up: every format added here
#: becomes newly deletable. It predates the Marqo swap and is kept narrow on
#: purpose. ``test_spoiler_extensions_are_a_strict_subset`` pins the
#: relationship so the two can't silently converge or diverge further.
SPOILER_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def is_image_attachment(attachment: SupportsAttachment) -> bool:
    """True for attachments that are images, regardless of size."""
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    return attachment.filename.lower().endswith(IMAGE_EXTENSIONS)


def is_classifiable(attachment: SupportsAttachment) -> bool:
    """True for attachments this service will fetch and classify.

    Attachments only — embeds are never classified. ``_has_image`` in the
    auto-react cog matches ``gifv``/``rich`` embeds whose images live on
    arbitrary external hosts; fetching those would aim the bot's outbound
    requests at member-supplied URLs (SSRF probing, IP-logging pixels,
    hostile payloads), so they are out of scope entirely.
    """
    return is_image_attachment(attachment) and attachment.size <= MAX_IMAGE_BYTES


def is_valid_threshold(value: float) -> bool:
    """Thresholds outside ``(0, 1]`` answer the same way for every image, so
    they disable the gate rather than loosening it. Shared with the route so
    the two can't drift."""
    return 0.0 < value <= 1.0


def load_settings(db_path: Path, guild_id: int) -> tuple[float, float]:
    """Return ``(threshold, sfw_threshold)`` for *guild_id*."""
    with open_db(db_path) as conn:
        return load_settings_with_conn(conn, guild_id)


def load_settings_with_conn(
    conn: sqlite3.Connection, guild_id: int
) -> tuple[float, float]:
    threshold = _float_config(conn, CONFIG_KEY_THRESHOLD, DEFAULT_THRESHOLD, guild_id)
    sfw_threshold = _float_config(
        conn, CONFIG_KEY_SFW_THRESHOLD, DEFAULT_SFW_THRESHOLD, guild_id
    )
    return threshold, sfw_threshold


@dataclass(frozen=True)
class SfwPolicy:
    """Settings for SFW nudity prevention."""

    mode: str = DEFAULT_SFW_MODE
    log_channel_id: int = 0
    exempt_channel_ids: frozenset[int] = frozenset()

    @property
    def is_active(self) -> bool:
        return self.mode in (SFW_MODE_LOG, SFW_MODE_ENFORCE)

    @property
    def deletes(self) -> bool:
        return self.mode == SFW_MODE_ENFORCE


def load_sfw_policy(db_path: Path, guild_id: int) -> SfwPolicy:
    """Read SFW-prevention settings, defaulting to fully off.

    An unrecognised mode is treated as ``off`` rather than guessed at — the
    failure mode of guessing wrong here is deleting members' photos.
    """
    with open_db(db_path) as conn:
        raw_mode = get_config_value(
            conn, CONFIG_KEY_SFW_MODE, DEFAULT_SFW_MODE, guild_id
        ).strip().lower()
        if raw_mode not in SFW_MODES:
            if raw_mode:
                log.warning("unknown %s (%r) — staying off", CONFIG_KEY_SFW_MODE, raw_mode)
            raw_mode = SFW_MODE_OFF
        try:
            log_channel_id = int(
                get_config_value(conn, CONFIG_KEY_SFW_LOG_CHANNEL, "0", guild_id)
            )
        except (TypeError, ValueError):
            log_channel_id = 0
        exempt = frozenset(
            get_config_id_set(conn, CONFIG_BUCKET_SFW_EXEMPT, guild_id)
        )
    return SfwPolicy(
        mode=raw_mode, log_channel_id=log_channel_id, exempt_channel_ids=exempt
    )


def load_observe_policy(db_path: Path, guild_id: int) -> bool:
    """Whether *guild_id* opted into scanning every age-gated upload.

    A plain bool rather than a policy object: there is exactly one decision
    here, and the scope it applies to (age-gated channels) is not
    configurable — widening it is what the privacy rule forbids, so there is
    nothing for a channel list to say.
    """
    with open_db(db_path) as conn:
        return parse_bool(get_config_value(conn, CONFIG_KEY_OBSERVE, "0", guild_id))


def _float_config(
    conn: sqlite3.Connection, key: str, default: float, guild_id: int
) -> float:
    raw = get_config_value(conn, key, str(default), guild_id)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        log.warning("bad float for %s (%r) — using %s", key, raw, default)
        return default
    if not is_valid_threshold(value):
        log.warning("out-of-range %s (%s) — using %s", key, value, default)
        return default
    return value


def evaluate(score: float, *, threshold: float = DEFAULT_THRESHOLD) -> bool:
    """Whether *score* clears *threshold*. Pure — no model, no I/O.

    Inclusive at the threshold: a dial set to 0.5 is read as "0.5 counts", not
    "anything above 0.5".
    """
    return score >= threshold


def top_detection(
    detections: list[Detection],
) -> tuple[str | None, float | None]:
    """Highest-scoring qualifying tag, for the recorded row's headline.

    Reported for description only — Marqo already decided the verdict — so a
    confident ``BELLY_EXPOSED`` never becomes the stated reason an image was
    judged explicit. No threshold is applied: NudeNet's own floor is the only
    bar that means anything for a label, and the configured threshold is a
    whole-image probability that has nothing to say about one body part.
    """
    qualifying = [d for d in detections if d.label in DEFAULT_LABEL_SET]
    if not qualifying:
        return None, None
    best = max(qualifying, key=lambda d: d.score)
    return best.label, best.score


def is_age_gated_channel(channel: object) -> bool:
    """Whether Discord itself age-gates this channel.

    Threads delegate to their parent, and channel types without the concept
    (DMs, group DMs) are False. Defaults to False on anything unexpected,
    which is the safe direction for both callers: no dataset is recorded and
    no tip is offered when we can't establish the age gate.
    """
    checker = getattr(channel, "is_nsfw", None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except Exception:  # noqa: BLE001 - a partial channel object must not raise here
        return False


def record_classification(
    conn: sqlite3.Connection,
    result: Classification,
    *,
    guild_id: int,
    channel_id: int,
    message_id: int,
    now: int | None = None,
) -> None:
    """Persist one verdict and its detections. Rides the caller's transaction.

    UNKNOWN results are not recorded — there is no verdict to interpret, and a
    row claiming ``verdict=0`` for an image nobody could read would poison the
    accuracy metrics this table exists to provide.
    """
    if result.is_unknown:
        return
    created_at = int(time.time()) if now is None else now
    conn.execute(
        """
        INSERT OR REPLACE INTO nsfw_classifications
            (message_id, attachment_id, guild_id, channel_id, verdict,
             marqo_score, top_label, top_score, model, threshold, label_set,
             inference_ms, bytes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            result.attachment_id,
            guild_id,
            channel_id,
            int(bool(result.verdict)),
            result.score,
            result.top_label,
            result.top_score,
            model_name(tagged=result.tagged),
            result.threshold,
            # No configurable label set governs a verdict any more, and
            # writing the tagger's vocabulary here would imply one did.
            "",
            result.inference_ms,
            result.size_bytes,
            created_at,
        ),
    )
    # Re-classification of the same attachment replaces rather than duplicates.
    conn.execute(
        "DELETE FROM nsfw_detections WHERE message_id = ? AND attachment_id = ?",
        (message_id, result.attachment_id),
    )
    conn.executemany(
        """
        INSERT INTO nsfw_detections
            (message_id, attachment_id, label, score, x1, y1, x2, y2)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                message_id,
                result.attachment_id,
                d.label,
                d.score,
                int(d.box.x1),
                int(d.box.y1),
                int(d.box.x2),
                int(d.box.y2),
            )
            for d in result.detections
        ],
    )


def record_block(
    conn: sqlite3.Connection, block: Block, *, now: int | None = None
) -> None:
    """Persist one destroyed image. Rides the caller's transaction.

    Unlike :func:`record_classification` this runs in **every** channel — it is
    the only record that a member's upload was taken away, and the channels
    where that is most likely to be a mistake are exactly the ones no
    classification row is written for.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO nsfw_blocks
            (message_id, attachment_id, guild_id, channel_id, author_id,
             filename, marqo_score, surface, action, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            block.message_id,
            block.attachment_id,
            block.guild_id,
            block.channel_id,
            block.author_id,
            block.filename,
            block.score,
            block.surface,
            block.action,
            int(time.time()) if now is None else now,
        ),
    )


def record_block_safely(db_path: Path, block: Block) -> None:
    """Record a block, swallowing storage failures.

    The audit trail failing must never change what happened to the member, and
    this is called from the enforcement path itself.
    """
    try:
        with open_db(db_path) as conn:
            record_block(conn, block)
    except sqlite3.Error as exc:
        log.warning("nsfw: failed to record block: %s", exc)


async def classify_attachment(
    attachment: SupportsAttachment,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    tag: bool = False,
) -> Classification:
    """Download, classify and return a verdict for one attachment.

    What is cached (and shared) is the **score**, not the verdict: the model's
    output doesn't depend on the threshold, only :func:`evaluate` does. So
    every consumer of an attachment shares one download and one inference no
    matter what bar each applies, and an admin who retunes the threshold can't
    be served a verdict computed under the old one.

    The cache holds the in-flight task rather than its result, so the three
    consumers that fire on the same message — which discord.py dispatches as
    concurrent tasks, not in sequence — await the same work instead of each
    starting their own.

    Pass ``tag=True`` to also run the NudeNet tagger over the same bytes. Only
    the recording path does, because tags are only ever written for age-gated
    channels — see :class:`MessageClassifier`.

    Never raises: a download failure, a decode failure or a model failure all
    return ``verdict=UNKNOWN`` so each consumer applies its own fallback.
    """
    unknown = Classification(
        attachment_id=attachment.id,
        verdict=UNKNOWN,
        size_bytes=attachment.size,
        threshold=threshold,
    )

    if not is_classifiable(attachment):
        return unknown

    try:
        score, detections, inference_ms, size_bytes = await _shared_infer(
            attachment, tag=tag
        )
    except Exception:  # noqa: BLE001 - already logged; every failure is UNKNOWN
        return unknown

    top_label, top_score = top_detection(detections)
    return Classification(
        attachment_id=attachment.id,
        verdict=evaluate(score, threshold=threshold),
        score=score,
        top_label=top_label,
        top_score=top_score,
        detections=detections,
        tagged=tag,
        inference_ms=inference_ms,
        size_bytes=size_bytes,
        threshold=threshold,
    )


async def _shared_infer(
    attachment: SupportsAttachment, *, tag: bool
) -> tuple[float, list[Detection], int, int]:
    """Download and run inference once per attachment, shared across callers.

    Raises on any failure; the failed task is evicted so a transient CDN error
    doesn't pin "unreadable" for the life of the process.
    """
    entry = _cache.get(attachment.id)
    # An entry computed without tags cannot satisfy a caller that wants them.
    # This should be unreachable — see the cache's docstring — so it replaces
    # the entry rather than trying to be clever about merging.
    if entry is not None and tag and not entry[0]:
        entry = None
        _cache.pop(attachment.id, None)

    if entry is not None:
        _cache.move_to_end(attachment.id)
        task = entry[1]
    else:
        task = asyncio.create_task(_download_and_infer(attachment, tag=tag))
        _cache[attachment.id] = (tag, task)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)

    try:
        return await asyncio.shield(task)
    except Exception:
        _cache.pop(attachment.id, None)
        raise


async def _download_and_infer(
    attachment: SupportsAttachment, *, tag: bool
) -> tuple[float, list[Detection], int, int]:
    try:
        raw = await asyncio.wait_for(
            attachment.read(), timeout=DOWNLOAD_TIMEOUT_SECONDS
        )
    except Exception as exc:
        log.warning("nsfw: download failed for %s: %s", attachment.id, exc)
        raise

    started = time.perf_counter()
    try:
        score = await asyncio.to_thread(_score, raw)
    except Exception as exc:
        log.warning("nsfw: inference failed for %s: %s", attachment.id, exc)
        raise
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    detections: list[Detection] = []
    if tag:
        # Tags are metrics. The verdict is already made and is what every
        # consumer acts on, so a tagger failure leaves the row unlabelled
        # rather than throwing the classification away.
        try:
            detections = await asyncio.to_thread(_tag, raw)
        except Exception as exc:  # noqa: BLE001 - metrics only
            log.warning("nsfw: tagging failed for %s: %s", attachment.id, exc)

    return score, detections, elapsed_ms, len(raw)


@dataclass
class MessageClassifier:
    """Classifier bound to one message, for one consumer.

    Built by :func:`classifier_for`. A small value object rather than a
    closure, so it copies the handful of ids it needs instead of capturing —
    and keeping alive — the whole ``discord.Message`` and its guild.

    ``channel_is_nsfw`` is derived here rather than supplied per call site.
    It drives both the recording scope and whether NudeNet runs at all, and
    the callers that used to pass it were asserting a precondition enforced by
    a guard several frames away in another module — true when written,
    silently wrong the moment those guards got reordered.

    Settings load lazily on first use and are then reused for the rest of the
    message, so constructing one is free. That matters: spoiler enforcement
    builds a classifier for every message in a watched channel but only
    consults it for an unspoilered image.
    """

    db_path: Path
    guild_id: int
    channel_id: int
    message_id: int
    channel_is_nsfw: bool
    strict: bool = False
    _threshold_task: "asyncio.Task[float] | None" = None

    async def _load_threshold(self) -> float:
        # The task, not the value: the auto-react cog gathers classify() over
        # every attachment, so with a plain value all N coroutines see None in
        # the same tick and each opens its own connection.
        if self._threshold_task is None:
            self._threshold_task = asyncio.create_task(self._read_threshold())
        try:
            return await asyncio.shield(self._threshold_task)
        except Exception:
            # Evict, like _shared_infer does: a cached failed task would
            # re-raise for every remaining attachment of the message, where the
            # plain value this replaced simply retried.
            self._threshold_task = None
            raise

    async def _read_threshold(self) -> float:
        threshold, sfw_threshold = await asyncio.to_thread(
            load_settings, self.db_path, self.guild_id
        )
        return sfw_threshold if self.strict else threshold

    async def __call__(self, attachment: SupportsAttachment) -> Classification:
        threshold = await self._load_threshold()
        # The age gate decides both whether tags are produced and whether a row
        # is written, from the same flag — so there is no arrangement of this
        # code that tags an image in general chat.
        result = await classify_attachment(
            attachment, threshold=threshold, tag=self.channel_is_nsfw
        )
        if result.is_unknown or not self.channel_is_nsfw:
            return result
        await asyncio.to_thread(self._record, result)
        return result

    def _record(self, result: Classification) -> None:
        try:
            with open_db(self.db_path) as conn:
                record_classification(
                    conn,
                    result,
                    guild_id=self.guild_id,
                    channel_id=self.channel_id,
                    message_id=self.message_id,
                )
        except sqlite3.Error as exc:
            # Metrics are a side effect; failing to record must never change
            # what a consumer does about the image.
            log.warning("nsfw: failed to record classification: %s", exc)


def classifier_for(
    db_path: Path, message: object, *, strict: bool = False
) -> MessageClassifier:
    """Bind a classifier to *message* for one consumer.

    The single entry point every consumer uses, so settings loading, the
    age-gate derivation and the recording-scope rule behave identically for
    all of them, and settings are read once per message rather than once per
    attachment.

    Pass ``strict=True`` for consumers that destroy content (SFW nudity
    prevention) — it applies the higher threshold.
    """
    channel = getattr(message, "channel", None)
    guild = getattr(message, "guild", None)
    return MessageClassifier(
        db_path=db_path,
        guild_id=getattr(guild, "id", 0) or 0,
        channel_id=getattr(channel, "id", 0) or 0,
        message_id=getattr(message, "id", 0) or 0,
        channel_is_nsfw=is_age_gated_channel(channel),
        strict=strict,
    )


def classifiable_attachments(message: object) -> list[SupportsAttachment]:
    """Every attachment on *message* this service is willing to look at."""
    return [
        att
        for att in getattr(message, "attachments", ()) or ()
        if is_classifiable(att)
    ]


async def observe_images(
    db_path: Path, message: object, *, enabled: bool
) -> list[Classification]:
    """Classify and record every image in an age-gated channel. Acts on nothing.

    The three gates only ever ask for a verdict when they might *do* something
    about it, so the metrics table has only ever seen the images that were
    about to be judged: in a spoiler-required channel that is the handful
    somebody forgot to spoiler, and in an age-gated channel with no spoiler
    rule it is nothing at all. Every question those rows exist to answer —
    where does the score distribution actually sit, what should the threshold
    be, how often is the model wrong — was being answered from the one sample
    guaranteed to be unrepresentative.

    This pass closes that gap by classifying the compliant uploads too. It is
    deliberately inert: it returns verdicts to nobody, and no caller may act on
    what it returns. A spoilered image in a spoiler channel is *compliant*, and
    the moment a verdict here could delete one, this stops being observation.

    Scope is age-gated channels and nothing else, because that is already the
    boundary recording lives inside — :class:`MessageClassifier` writes a row
    only when ``channel_is_nsfw``, and the same flag turns the tagger on. So
    this widens *which* images inside that boundary are seen; it does not move
    the boundary. Off unless a guild turns it on, because it does mean the
    most sensitive table this bot holds grows to cover ordinary compliant
    posts rather than only the ones a gate had to judge.

    Never raises: this runs beside message handling and must not be able to
    take it down. A failure to observe is a missing metrics row, nothing more.
    """
    if not enabled:
        return []
    if not is_age_gated_channel(getattr(message, "channel", None)):
        return []
    # Bots and webhooks are exempt, as they are for SFW prevention, but here
    # the reason is measurement rather than mercy: the Guess game re-uploads a
    # member's own submission as SPOILER_guess_full.jpg, so counting the bot's
    # posts would enter the same picture into the sample twice and skew the
    # distribution these rows exist to describe.
    author = getattr(message, "author", None)
    if getattr(author, "bot", False) or getattr(message, "webhook_id", None):
        return []
    attachments = classifiable_attachments(message)
    if not attachments:
        return []

    # The standard threshold, not the strict one: these rows sit alongside the
    # gates' own, and a mixed table where the verdict column means a different
    # bar per row would be worse than no rows.
    classify = classifier_for(db_path, message)
    results = await asyncio.gather(
        *(classify(att) for att in attachments), return_exceptions=True
    )
    out: list[Classification] = []
    for att, result in zip(attachments, results):
        if isinstance(result, BaseException):
            log.warning("nsfw: observing %s failed: %s", att.id, result)
            continue
        out.append(result)
    return out


def _score(raw: bytes) -> float:
    """Blocking Marqo inference, run off the event loop.

    onnxruntime blocks in C++; calling it inline would stall the bot's
    heartbeat for the duration of every classification.
    """
    from bot_modules.services.marqo_nsfw import score_bytes  # noqa: PLC0415

    return score_bytes(raw)


def _tag(raw: bytes) -> list[Detection]:
    """Blocking NudeNet detection, for labels and boxes only."""
    from bot_modules.services.guess_nudenet import detect_bytes  # noqa: PLC0415
    from bot_modules.services.guess_pipeline import (  # noqa: PLC0415
        merge_sex_act_detections,
    )

    return merge_sex_act_detections(detect_bytes(raw))


def clear_cache() -> None:
    """Drop the in-process cache (tests; config changes)."""
    _cache.clear()
