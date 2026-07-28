"""Shared NSFW classifier — verdicts, failure direction, and recording scope.

The service exists so three consumers with *different* failure tolerances can
share one classification, so the tests that matter most here are the ones
pinning UNKNOWN as distinct from False, and recording as scoped to age-gated
channels only.
"""
from __future__ import annotations

import asyncio

import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.services.guess_models import BoundingBox, Detection
from bot_modules.services.nsfw_classifier_service import (
    DEFAULT_LABEL_SET,
    DEFAULT_SFW_THRESHOLD,
    DEFAULT_THRESHOLD,
    UNKNOWN,
    Classification,
    classify_attachment,
    clear_cache,
    evaluate,
    is_classifiable,
    load_settings,
    parse_label_set,
    record_classification,
    serialize_label_set,
    should_record,
)

GUILD = 1234
CHANNEL = 5678
MESSAGE = 9012


def det(label: str, score: float) -> Detection:
    return Detection(label=label, score=score, box=BoundingBox(0, 0, 10, 20))


class FakeAttachment:
    """Structural stand-in for discord.Attachment (see SupportsAttachment)."""

    def __init__(
        self,
        *,
        attachment_id: int = 1,
        data: bytes = b"imagebytes",
        size: int | None = None,
        filename: str = "pic.png",
        content_type: str | None = "image/png",
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.id = attachment_id
        self.filename = filename
        self.content_type = content_type
        self.size = len(data) if size is None else size
        self._data = data
        self._error = error
        self._delay = delay
        self.reads = 0

    async def read(self) -> bytes:
        self.reads += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._data


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def patched_detect(monkeypatch):
    """Replace inference with a canned detection list."""

    def _install(detections: list[Detection] | Exception):
        def fake(_raw: bytes) -> list[Detection]:
            if isinstance(detections, Exception):
                raise detections
            return detections

        monkeypatch.setattr(
            "bot_modules.services.nsfw_classifier_service._detect", fake
        )

    return _install


# --------------------------------------------------------------------------
# evaluate() — the pure verdict
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "score", "expected"),
    [
        pytest.param("FEMALE_BREAST_EXPOSED", 0.9, True, id="exposed-qualifies"),
        pytest.param("MALE_GENITALIA_EXPOSED", 0.9, True, id="genitalia-qualifies"),
        pytest.param("ANUS_EXPOSED", 0.9, True, id="anus-qualifies"),
        pytest.param("BUTTOCKS_EXPOSED", 0.9, True, id="buttocks-qualifies"),
        pytest.param("SEX_ACT", 0.9, True, id="sex-act-qualifies"),
        pytest.param("FEMALE_BREAST_COVERED", 0.99, False, id="covered-does-not"),
        pytest.param("BUTTOCKS_COVERED", 0.99, False, id="covered-buttocks-does-not"),
        pytest.param("FEMALE_GENITALIA_COVERED", 0.99, False, id="covered-gen-does-not"),
        pytest.param("BELLY_EXPOSED", 0.99, False, id="belly-does-not"),
        pytest.param("ARMPITS_EXPOSED", 0.99, False, id="armpits-does-not"),
        pytest.param("FACE_FEMALE", 0.99, False, id="face-does-not"),
    ],
)
def test_evaluate_label_membership(label, score, expected):
    verdict, _, _ = evaluate([det(label, score)])
    assert verdict is expected


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        pytest.param(0.49, False, id="below-threshold"),
        pytest.param(0.5, True, id="at-threshold-inclusive"),
        pytest.param(0.51, True, id="above-threshold"),
    ],
)
def test_evaluate_threshold_boundary(score, expected):
    verdict, _, _ = evaluate([det("FEMALE_BREAST_EXPOSED", score)])
    assert verdict is expected


def test_evaluate_reports_highest_scoring_qualifying_label():
    # A very confident non-qualifying detection must never be reported as the
    # reason an image was judged explicit.
    verdict, label, score = evaluate([
        det("BELLY_EXPOSED", 0.99),
        det("FEMALE_BREAST_EXPOSED", 0.6),
        det("ANUS_EXPOSED", 0.8),
    ])
    assert verdict is True
    assert label == "ANUS_EXPOSED"
    assert score == 0.8


def test_evaluate_no_detections_is_not_explicit():
    verdict, label, score = evaluate([])
    assert (verdict, label, score) == (False, None, None)


def test_evaluate_honors_a_stricter_threshold():
    # The SFW consumer runs the same detections at a higher bar and must get
    # the opposite answer — this is the knob that keeps innocent photos alive.
    detections = [det("BUTTOCKS_EXPOSED", 0.6)]
    assert evaluate(detections, threshold=DEFAULT_THRESHOLD)[0] is True
    assert evaluate(detections, threshold=DEFAULT_SFW_THRESHOLD)[0] is False


def test_evaluate_honors_a_custom_label_set():
    detections = [det("FEMALE_BREAST_COVERED", 0.9)]
    assert evaluate(detections)[0] is False
    assert evaluate(detections, label_set=frozenset({"FEMALE_BREAST_COVERED"}))[0] is True


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("", DEFAULT_LABEL_SET, id="empty-falls-back"),
        pytest.param("   ", DEFAULT_LABEL_SET, id="whitespace-falls-back"),
        pytest.param(None, DEFAULT_LABEL_SET, id="none-falls-back"),
        pytest.param(",,", DEFAULT_LABEL_SET, id="separators-only-falls-back"),
        pytest.param("SEX_ACT", frozenset({"SEX_ACT"}), id="single"),
        pytest.param(
            "sex_act, anus_exposed",
            frozenset({"SEX_ACT", "ANUS_EXPOSED"}),
            id="case-and-space-normalised",
        ),
    ],
)
def test_parse_label_set(raw, expected):
    # An empty set would match nothing and silently disable every consumer.
    assert parse_label_set(raw) == expected


def test_serialize_label_set_is_sorted():
    assert serialize_label_set(frozenset({"B", "A", "C"})) == "A,B,C"


def test_load_settings_defaults(sync_db_path):
    threshold, sfw_threshold, labels = load_settings(sync_db_path, GUILD)
    assert threshold == DEFAULT_THRESHOLD
    assert sfw_threshold == DEFAULT_SFW_THRESHOLD
    assert labels == DEFAULT_LABEL_SET


def test_load_settings_reads_configured_values(sync_db_path):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, "nsfw_classifier_threshold", "0.4", GUILD)
        set_config_value(conn, "nsfw_classifier_sfw_threshold", "0.9", GUILD)
        set_config_value(conn, "nsfw_classifier_labels", "SEX_ACT", GUILD)

    threshold, sfw_threshold, labels = load_settings(sync_db_path, GUILD)
    assert threshold == 0.4
    assert sfw_threshold == 0.9
    assert labels == frozenset({"SEX_ACT"})


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("not-a-number", id="unparseable"),
        pytest.param("0", id="zero-matches-everything"),
        pytest.param("-0.5", id="negative"),
        pytest.param("1.5", id="above-one-matches-nothing"),
    ],
)
def test_load_settings_rejects_out_of_range_threshold(sync_db_path, bad):
    # A threshold outside (0, 1] answers the same way for every image, which
    # would silently disable the gate rather than loosen it.
    with open_db(sync_db_path) as conn:
        set_config_value(conn, "nsfw_classifier_threshold", bad, GUILD)

    threshold, _, _ = load_settings(sync_db_path, GUILD)
    assert threshold == DEFAULT_THRESHOLD


# --------------------------------------------------------------------------
# is_classifiable — attachments only, size-capped
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "content_type", "expected"),
    [
        pytest.param("a.png", "image/png", True, id="content-type-image"),
        pytest.param("a.PNG", None, True, id="extension-uppercase"),
        pytest.param("a.jpeg", None, True, id="extension-jpeg"),
        pytest.param("a.webp", None, True, id="extension-webp"),
        pytest.param("a.txt", "text/plain", False, id="text"),
        pytest.param("a.mp4", "video/mp4", False, id="video"),
        pytest.param("a.pdf", None, False, id="no-type-no-image-extension"),
    ],
)
def test_is_classifiable_types(filename, content_type, expected):
    att = FakeAttachment(filename=filename, content_type=content_type)
    assert is_classifiable(att) is expected


def test_is_classifiable_rejects_oversized():
    att = FakeAttachment(size=26 * 1024 * 1024)
    assert is_classifiable(att) is False


# --------------------------------------------------------------------------
# classify_attachment — failure direction and caching
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classify_returns_verdict_and_metrics(patched_detect):
    patched_detect([det("FEMALE_BREAST_EXPOSED", 0.8)])
    att = FakeAttachment(data=b"x" * 100)

    result = await classify_attachment(att)

    assert result.verdict is True
    assert result.top_label == "FEMALE_BREAST_EXPOSED"
    assert result.top_score == 0.8
    assert result.size_bytes == 100
    assert result.threshold == DEFAULT_THRESHOLD
    assert result.inference_ms >= 0


@pytest.mark.asyncio
async def test_classify_non_explicit_is_false_not_unknown(patched_detect):
    # "We looked, it isn't explicit" must be distinguishable from "we couldn't
    # tell" — the three consumers branch differently on the two.
    patched_detect([det("BELLY_EXPOSED", 0.99)])

    result = await classify_attachment(FakeAttachment())

    assert result.verdict is False
    assert result.is_unknown is False


@pytest.mark.asyncio
async def test_download_failure_is_unknown(patched_detect):
    patched_detect([det("SEX_ACT", 0.99)])
    att = FakeAttachment(error=RuntimeError("cdn is having a day"))

    result = await classify_attachment(att)

    assert result.verdict is UNKNOWN
    assert result.is_unknown is True


@pytest.mark.asyncio
async def test_inference_failure_is_unknown(patched_detect):
    patched_detect(ValueError("undecodable image"))

    result = await classify_attachment(FakeAttachment())

    assert result.verdict is UNKNOWN


@pytest.mark.asyncio
async def test_unclassifiable_attachment_is_unknown_without_downloading(
    patched_detect,
):
    patched_detect([det("SEX_ACT", 0.99)])
    att = FakeAttachment(filename="notes.txt", content_type="text/plain")

    result = await classify_attachment(att)

    assert result.verdict is UNKNOWN
    assert att.reads == 0


@pytest.mark.asyncio
async def test_download_timeout_is_unknown(patched_detect, monkeypatch):
    monkeypatch.setattr(
        "bot_modules.services.nsfw_classifier_service.DOWNLOAD_TIMEOUT_SECONDS",
        0.01,
    )
    patched_detect([det("SEX_ACT", 0.99)])

    result = await classify_attachment(FakeAttachment(delay=0.2))

    assert result.verdict is UNKNOWN


@pytest.mark.asyncio
async def test_second_consumer_reuses_the_cached_verdict(patched_detect):
    # Spoiler enforcement and auto-react both fire on the same on_message;
    # the image must be downloaded and classified once between them.
    patched_detect([det("SEX_ACT", 0.9)])
    att = FakeAttachment()

    first = await classify_attachment(att)
    second = await classify_attachment(att)

    assert first.verdict is True
    assert second.verdict is True
    assert att.reads == 1


@pytest.mark.asyncio
async def test_different_threshold_bypasses_the_cache(patched_detect):
    # The SFW consumer runs a stricter threshold over the same image and must
    # not be handed the permissive verdict.
    patched_detect([det("BUTTOCKS_EXPOSED", 0.6)])
    att = FakeAttachment()

    permissive = await classify_attachment(att, threshold=0.5)
    strict = await classify_attachment(att, threshold=0.9)

    assert permissive.verdict is True
    assert strict.verdict is False


@pytest.mark.asyncio
async def test_unknown_results_are_not_cached(patched_detect):
    # A transient CDN failure must not pin UNKNOWN for the life of the process.
    att = FakeAttachment(error=RuntimeError("transient"))
    patched_detect([det("SEX_ACT", 0.9)])

    assert (await classify_attachment(att)).is_unknown is True

    att._error = None
    assert (await classify_attachment(att)).verdict is True


# --------------------------------------------------------------------------
# recording — scoped to age-gated channels
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("channel_is_nsfw", "expected"),
    [
        pytest.param(True, True, id="nsfw-channel-records"),
        pytest.param(False, False, id="sfw-channel-does-not"),
    ],
)
def test_should_record_only_in_age_gated_channels(channel_is_nsfw, expected):
    # Classification runs everywhere (SFW prevention needs it), but no dataset
    # is built out of general chat.
    assert should_record(channel_is_nsfw) is expected


def _classification(**overrides) -> Classification:
    base = dict(
        attachment_id=42,
        verdict=True,
        top_label="SEX_ACT",
        top_score=0.91,
        detections=[det("SEX_ACT", 0.91), det("BELLY_EXPOSED", 0.3)],
        inference_ms=74,
        size_bytes=2048,
    )
    base.update(overrides)
    return Classification(**base)


def test_record_writes_summary_and_detections(sync_db_path):
    with open_db(sync_db_path) as conn:
        record_classification(
            conn,
            _classification(),
            guild_id=GUILD,
            channel_id=CHANNEL,
            message_id=MESSAGE,
            now=1700000000,
        )

    with open_db(sync_db_path) as conn:
        row = conn.execute(
            "SELECT * FROM nsfw_classifications WHERE message_id=?", (MESSAGE,)
        ).fetchone()
        detections = conn.execute(
            "SELECT label, score FROM nsfw_detections WHERE message_id=? ORDER BY label",
            (MESSAGE,),
        ).fetchall()

    assert row["verdict"] == 1
    assert row["top_label"] == "SEX_ACT"
    assert row["inference_ms"] == 74
    assert row["bytes"] == 2048
    assert row["created_at"] == 1700000000
    # Threshold and label set are stored per row so the data stays readable
    # after a retune.
    assert row["threshold"] == DEFAULT_THRESHOLD
    assert row["label_set"] == serialize_label_set(DEFAULT_LABEL_SET)
    assert row["model"] == "320n"
    # Near-misses are kept — a threshold sweep needs the ones that didn't
    # qualify, not just the ones that did.
    assert [d["label"] for d in detections] == ["BELLY_EXPOSED", "SEX_ACT"]


def test_record_stores_no_author_id(sync_db_path):
    # Authorship joins through messages rather than being duplicated here.
    with open_db(sync_db_path) as conn:
        columns = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(nsfw_classifications)")
        }
    assert "author_id" not in columns


def test_record_skips_unknown_verdicts(sync_db_path):
    # A row claiming verdict=0 for an image nobody could read would poison the
    # accuracy metrics this table exists to provide.
    with open_db(sync_db_path) as conn:
        record_classification(
            conn,
            _classification(verdict=UNKNOWN, top_label=None, top_score=None),
            guild_id=GUILD,
            channel_id=CHANNEL,
            message_id=MESSAGE,
        )
        count = conn.execute(
            "SELECT COUNT(*) c FROM nsfw_classifications"
        ).fetchone()["c"]

    assert count == 0


def test_record_is_idempotent_per_attachment(sync_db_path):
    with open_db(sync_db_path) as conn:
        for _ in range(2):
            record_classification(
                conn,
                _classification(),
                guild_id=GUILD,
                channel_id=CHANNEL,
                message_id=MESSAGE,
            )
        summaries = conn.execute(
            "SELECT COUNT(*) c FROM nsfw_classifications"
        ).fetchone()["c"]
        detections = conn.execute(
            "SELECT COUNT(*) c FROM nsfw_detections"
        ).fetchone()["c"]

    assert summaries == 1
    assert detections == 2  # replaced, not duplicated
