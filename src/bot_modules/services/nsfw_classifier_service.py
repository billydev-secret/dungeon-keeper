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

from bot_modules.core.db_utils import get_config_value, open_db
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

#: Confidence a qualifying label must reach. Consumers that destroy content
#: use a higher one — see CONFIG_KEY_SFW_THRESHOLD.
DEFAULT_THRESHOLD = 0.5

#: SFW nudity prevention deletes a member's upload on a positive, so it demands
#: more certainty than merely qualifying a post for coins.
DEFAULT_SFW_THRESHOLD = 0.75

CONFIG_KEY_THRESHOLD = "nsfw_classifier_threshold"
CONFIG_KEY_SFW_THRESHOLD = "nsfw_classifier_sfw_threshold"
CONFIG_KEY_LABEL_SET = "nsfw_classifier_labels"

#: Images larger than this are not downloaded. Guards against a member pinning
#: the bot's bandwidth with a huge upload; Discord's own limit is well under
#: this for most users.
MAX_IMAGE_BYTES = 25 * 1024 * 1024

#: Seconds to wait for an attachment download before giving up as UNKNOWN.
DOWNLOAD_TIMEOUT_SECONDS = 10.0

#: Verdict for "we could not tell" — distinct from False ("we looked, it isn't
#: explicit"). Callers MUST branch on this rather than treating it as False.
UNKNOWN = None

_CACHE_MAX = 512
_cache: OrderedDict[int, "Classification"] = OrderedDict()


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


def is_classifiable(attachment: SupportsAttachment) -> bool:
    """True for attachments this service will fetch and classify.

    Attachments only — embeds are never classified. ``_has_image`` in the
    auto-react cog matches ``gifv``/``rich`` embeds whose images live on
    arbitrary external hosts; fetching those would aim the bot's outbound
    requests at member-supplied URLs (SSRF probing, IP-logging pixels,
    hostile payloads), so they are out of scope entirely.
    """
    if attachment.size > MAX_IMAGE_BYTES:
        return False
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    return attachment.filename.lower().endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
    )


def parse_label_set(raw: str | None) -> frozenset[str]:
    """Parse a comma-separated label set, falling back to the default.

    An empty or whitespace-only value would otherwise produce a set that
    nothing can ever match, silently disabling every consumer.
    """
    if not raw:
        return DEFAULT_LABEL_SET
    labels = {part.strip().upper() for part in raw.split(",") if part.strip()}
    return frozenset(labels) if labels else DEFAULT_LABEL_SET


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


def _float_config(
    conn: sqlite3.Connection, key: str, default: float, guild_id: int
) -> float:
    raw = get_config_value(conn, key, str(default), guild_id)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        log.warning("bad float for %s (%r) — using %s", key, raw, default)
        return default
    # A threshold outside (0, 1] would make the classifier answer the same way
    # for every image; treat it as misconfiguration rather than honoring it.
    if not 0.0 < value <= 1.0:
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


def should_record(channel_is_nsfw: bool) -> bool:
    """Whether a verdict for this channel is persisted.

    Only age-gated channels build a dataset. Classification still runs
    everywhere — SFW prevention needs a verdict in general chat — it just
    leaves no trace there.
    """
    return channel_is_nsfw


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
    use_cache: bool = True,
) -> Classification:
    """Download, classify and return a verdict for one attachment.

    Cached by attachment id so the consumers that fire on the same message
    (spoiler enforcement, then auto-react) classify once between them. The
    cache is keyed on identity alone, so a caller needing a *different*
    threshold than the cached one must pass ``use_cache=False`` — the SFW
    consumer does exactly that.

    Never raises: a download failure, a decode failure or a model failure all
    return ``verdict=UNKNOWN`` so each consumer applies its own fallback.
    """
    if use_cache:
        cached = _cache.get(attachment.id)
        if cached is not None and cached.threshold == threshold:
            _cache.move_to_end(attachment.id)
            return cached

    result = await _classify_uncached(
        attachment, threshold=threshold, label_set=label_set
    )

    if use_cache and not result.is_unknown:
        _cache[attachment.id] = result
        _cache.move_to_end(attachment.id)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return result


async def _classify_uncached(
    attachment: SupportsAttachment,
    *,
    threshold: float,
    label_set: frozenset[str],
) -> Classification:
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
        raw = await asyncio.wait_for(
            attachment.read(), timeout=DOWNLOAD_TIMEOUT_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 - incl. TimeoutError; never fail a caller
        log.warning("nsfw: download failed for %s: %s", attachment.id, exc)
        return unknown

    started = time.perf_counter()
    try:
        detections = await asyncio.to_thread(_detect, raw)
    except Exception as exc:  # noqa: BLE001 - a bad decode must not kill on_message
        log.warning("nsfw: inference failed for %s: %s", attachment.id, exc)
        return unknown
    inference_ms = int((time.perf_counter() - started) * 1000)

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
        size_bytes=len(raw),
        threshold=threshold,
        label_set=label_set,
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
