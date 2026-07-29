"""Shared NSFW image classifier — one verdict per attachment, three consumers.

Reaction tipping, spoiler enforcement and SFW nudity prevention all need to
know whether an uploaded image is explicit. They fire off the same
``on_message``, so classification happens **once** per attachment and the
result is shared (see :func:`classify_attachment`'s cache).

The three consumers disagree about which way a *failure* should fall, so this
module never picks for them: an unreadable or unclassifiable image yields
``verdict=None`` (:data:`UNKNOWN`), and each caller applies its own fallback.
Tipping reacts anyway (a CDN hiccup must not cost a poster), spoiler
enforcement deletes (preserving today's behavior), SFW prevention does nothing
(never delete on a failed read).

Gating vs recording deliberately differ: classification runs everywhere,
because SFW prevention needs a verdict in every channel, but detections are
recorded only for uploads in Discord-age-gated channels. See
docs/nsfw_classifier_spec.md.

nudenet is imported lazily (via guess_nudenet), so importing this module is
free and safe on machines without the model.
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

from bot_modules.core.db_utils import get_config_id_set, get_config_value, open_db
from bot_modules.services.guess_models import Detection

log = logging.getLogger("dungeonkeeper.nsfw")

MODEL_NAME = "320n"

#: Labels that qualify an image as explicit. Exposed nudity only — the paired
#: ``*_COVERED`` labels (lingerie, swimwear, implied) deliberately do not
#: qualify. ``SEX_ACT`` is synthesised by guess_pipeline.merge_sex_act_detections
#: when two different genital labels overlap.
DEFAULT_LABEL_SET: frozenset[str] = frozenset({
    "MALE_GENITALIA_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "SEX_ACT",
})

#: Labels a guild may opt into on top of the defaults. Together with
#: DEFAULT_LABEL_SET this is the detector's vocabulary as far as this feature
#: is concerned — it lives here rather than in the web route because a label
#: outside it matches nothing and silently disables detection for that entry,
#: which is exactly what parse_label_set's empty-set guard exists to prevent.
OPTIONAL_LABELS: frozenset[str] = frozenset({
    "FEMALE_BREAST_COVERED",
    "BUTTOCKS_COVERED",
    "FEMALE_GENITALIA_COVERED",
    "MALE_GENITALIA_COVERED",
    "BELLY_EXPOSED",
    "ARMPITS_EXPOSED",
})

#: Confidence a qualifying label must reach. Consumers that destroy content
#: use a higher one — see CONFIG_KEY_SFW_THRESHOLD.
DEFAULT_THRESHOLD = 0.5

#: SFW nudity prevention deletes a member's upload on a positive, so it demands
#: more certainty than merely qualifying a post for coins.
DEFAULT_SFW_THRESHOLD = 0.75

CONFIG_KEY_THRESHOLD = "nsfw_classifier_threshold"
CONFIG_KEY_SFW_THRESHOLD = "nsfw_classifier_sfw_threshold"
CONFIG_KEY_LABEL_SET = "nsfw_classifier_labels"
CONFIG_KEY_SFW_MODE = "nsfw_sfw_prevention_mode"
CONFIG_KEY_SFW_LOG_CHANNEL = "nsfw_sfw_prevention_log_channel_id"
CONFIG_BUCKET_SFW_EXEMPT = "nsfw_prevention_exempt_channels"

#: SFW nudity prevention is the only thing here that destroys a member's
#: upload, so it ships **off** and is turned on from the dashboard. ``log``
#: is the shakedown mode: it reports what it *would* have deleted, so real
#: accuracy can be measured against real traffic before anything is lost.
SFW_MODE_OFF = "off"
SFW_MODE_LOG = "log"
SFW_MODE_ENFORCE = "enforce"
SFW_MODES = (SFW_MODE_OFF, SFW_MODE_LOG, SFW_MODE_ENFORCE)
DEFAULT_SFW_MODE = SFW_MODE_OFF

#: Images larger than this are not downloaded. Guards against a member pinning
#: the bot's bandwidth with a huge upload; Discord's own limit is well under
#: this for most users.
MAX_IMAGE_BYTES = 25 * 1024 * 1024

#: Seconds to wait for an attachment download before giving up as UNKNOWN.
DOWNLOAD_TIMEOUT_SECONDS = 10.0

#: Verdict for "we could not tell" — distinct from False ("we looked, it isn't
#: explicit"). Callers MUST branch on this rather than treating it as False.
UNKNOWN = None

#: attachment id -> in-flight or completed (detections, inference_ms, bytes).
#: Holds the task so concurrent consumers share one download+inference; holds
#: detections rather than verdicts so it stays valid across thresholds and
#: label-set edits. A completed task retains only the small detection list.
_CACHE_MAX = 512
_cache: OrderedDict[int, "asyncio.Task[tuple[list[Detection], int, int]]"] = (
    OrderedDict()
)


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


@dataclass
class Classification:
    """One attachment's verdict plus everything needed to interpret it later."""

    attachment_id: int
    verdict: bool | None
    top_label: str | None = None
    top_score: float | None = None
    detections: list[Detection] = field(default_factory=list)
    inference_ms: int = 0
    size_bytes: int = 0
    threshold: float = DEFAULT_THRESHOLD
    label_set: frozenset[str] = DEFAULT_LABEL_SET
    model: str = MODEL_NAME

    @property
    def is_unknown(self) -> bool:
        return self.verdict is UNKNOWN


#: The one definition of "this attachment is an image". Every consumer routes
#: through it — ``post_monitoring.attachment_is_image`` delegates here and the
#: auto-react cog filters with :func:`is_classifiable` — so the three copies
#: that used to disagree (over ``.tiff``, and over attachments Discord serves
#: with no ``content_type``) can no longer drift apart.
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff")


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


def parse_label_set(raw: str | None) -> frozenset[str]:
    """Parse a comma-separated label set, falling back to the default.

    An empty or whitespace-only value would otherwise produce a set that
    nothing can ever match, silently disabling every consumer.
    """
    if not raw:
        return DEFAULT_LABEL_SET
    labels = {part.strip().upper() for part in raw.split(",") if part.strip()}
    return frozenset(labels) if labels else DEFAULT_LABEL_SET


def known_labels() -> frozenset[str]:
    """Every label a guild may configure as qualifying."""
    return DEFAULT_LABEL_SET | OPTIONAL_LABELS


def is_valid_threshold(value: float) -> bool:
    """Thresholds outside ``(0, 1]`` answer the same way for every image, so
    they disable the gate rather than loosening it. Shared with the route so
    the two can't drift."""
    return 0.0 < value <= 1.0


def serialize_label_set(labels: frozenset[str]) -> str:
    """Stable text form for storage — sorted so rows compare as strings."""
    return ",".join(sorted(labels))


def load_settings(
    db_path: Path, guild_id: int
) -> tuple[float, float, frozenset[str]]:
    """Return ``(threshold, sfw_threshold, label_set)`` for *guild_id*."""
    with open_db(db_path) as conn:
        return load_settings_with_conn(conn, guild_id)


def load_settings_with_conn(
    conn: sqlite3.Connection, guild_id: int
) -> tuple[float, float, frozenset[str]]:
    threshold = _float_config(conn, CONFIG_KEY_THRESHOLD, DEFAULT_THRESHOLD, guild_id)
    sfw_threshold = _float_config(
        conn, CONFIG_KEY_SFW_THRESHOLD, DEFAULT_SFW_THRESHOLD, guild_id
    )
    labels = parse_label_set(
        get_config_value(conn, CONFIG_KEY_LABEL_SET, "", guild_id) or None
    )
    return threshold, sfw_threshold, labels


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


def evaluate(
    detections: list[Detection],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    label_set: frozenset[str] = DEFAULT_LABEL_SET,
) -> tuple[bool, str | None, float | None]:
    """Reduce raw detections to ``(is_explicit, top_label, top_score)``.

    Pure — no model, no I/O. The top label reported is the highest-scoring
    *qualifying* detection, so a confident ``BELLY_EXPOSED`` never becomes the
    stated reason an image was judged explicit.
    """
    qualifying = [
        d for d in detections if d.label in label_set and d.score >= threshold
    ]
    if not qualifying:
        return False, None, None
    best = max(qualifying, key=lambda d: d.score)
    return True, best.label, best.score


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
             top_label, top_score, model, threshold, label_set,
             inference_ms, bytes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            result.attachment_id,
            guild_id,
            channel_id,
            int(bool(result.verdict)),
            result.top_label,
            result.top_score,
            result.model,
            result.threshold,
            serialize_label_set(result.label_set),
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


async def classify_attachment(
    attachment: SupportsAttachment,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    label_set: frozenset[str] = DEFAULT_LABEL_SET,
) -> Classification:
    """Download, classify and return a verdict for one attachment.

    What is cached (and shared) is the **detections**, not the verdict: the
    model's output doesn't depend on the threshold or the label set, only
    :func:`evaluate` does. So every consumer of an attachment shares one
    download and one inference no matter what bar each applies, and an admin
    editing the label set can't be served a verdict computed under the old one.

    The cache holds the in-flight task rather than its result, so the three
    consumers that fire on the same message — which discord.py dispatches as
    concurrent tasks, not in sequence — await the same work instead of each
    starting their own.

    Never raises: a download failure, a decode failure or a model failure all
    return ``verdict=UNKNOWN`` so each consumer applies its own fallback.
    """
    unknown = Classification(
        attachment_id=attachment.id,
        verdict=UNKNOWN,
        size_bytes=attachment.size,
        threshold=threshold,
        label_set=label_set,
    )

    if not is_classifiable(attachment):
        return unknown

    try:
        detections, inference_ms, size_bytes = await _shared_detect(attachment)
    except Exception:  # noqa: BLE001 - already logged; every failure is UNKNOWN
        return unknown

    verdict, top_label, top_score = evaluate(
        detections, threshold=threshold, label_set=label_set
    )
    return Classification(
        attachment_id=attachment.id,
        verdict=verdict,
        top_label=top_label,
        top_score=top_score,
        detections=detections,
        inference_ms=inference_ms,
        size_bytes=size_bytes,
        threshold=threshold,
        label_set=label_set,
    )


async def _shared_detect(
    attachment: SupportsAttachment,
) -> tuple[list[Detection], int, int]:
    """Download and run inference once per attachment, shared across callers.

    Returns ``(detections, inference_ms, size_bytes)``. Raises on any failure;
    the failed task is evicted so a transient CDN error doesn't pin "unreadable"
    for the life of the process.
    """
    task = _cache.get(attachment.id)
    if task is not None:
        _cache.move_to_end(attachment.id)
    else:
        task = asyncio.create_task(_download_and_detect(attachment))
        _cache[attachment.id] = task
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)

    try:
        return await asyncio.shield(task)
    except Exception:
        _cache.pop(attachment.id, None)
        raise


async def _download_and_detect(
    attachment: SupportsAttachment,
) -> tuple[list[Detection], int, int]:
    try:
        raw = await asyncio.wait_for(
            attachment.read(), timeout=DOWNLOAD_TIMEOUT_SECONDS
        )
    except Exception as exc:
        log.warning("nsfw: download failed for %s: %s", attachment.id, exc)
        raise

    started = time.perf_counter()
    try:
        detections = await asyncio.to_thread(_detect, raw)
    except Exception as exc:
        log.warning("nsfw: inference failed for %s: %s", attachment.id, exc)
        raise
    return detections, int((time.perf_counter() - started) * 1000), len(raw)


@dataclass
class MessageClassifier:
    """Classifier bound to one message, for one consumer.

    Built by :func:`classifier_for`. A small value object rather than a
    closure, so it copies the handful of ids it needs instead of capturing —
    and keeping alive — the whole ``discord.Message`` and its guild.

    ``channel_is_nsfw`` is derived here rather than supplied per call site.
    It drives the recording-scope rule, and the callers that used to pass it
    were asserting a precondition enforced by a guard several frames away in
    another module — true when written, silently wrong the moment those guards
    got reordered.

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
    _settings: tuple[float, frozenset[str]] | None = None

    async def _load_settings(self) -> tuple[float, frozenset[str]]:
        if self._settings is None:
            threshold, sfw_threshold, label_set = await asyncio.to_thread(
                load_settings, self.db_path, self.guild_id
            )
            self._settings = (
                sfw_threshold if self.strict else threshold,
                label_set,
            )
        return self._settings

    async def __call__(self, attachment: SupportsAttachment) -> Classification:
        threshold, label_set = await self._load_settings()
        result = await classify_attachment(
            attachment, threshold=threshold, label_set=label_set
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


def _detect(raw: bytes) -> list[Detection]:
    """Blocking inference, run off the event loop by :func:`classify_attachment`.

    onnxruntime blocks in C++; calling it inline would stall the bot's
    heartbeat for the duration of every classification.
    """
    from bot_modules.services.guess_nudenet import detect_bytes  # noqa: PLC0415
    from bot_modules.services.guess_pipeline import (  # noqa: PLC0415
        merge_sex_act_detections,
    )

    return merge_sex_act_detections(detect_bytes(raw))


def clear_cache() -> None:
    """Drop the in-process verdict cache (tests; config changes)."""
    _cache.clear()
