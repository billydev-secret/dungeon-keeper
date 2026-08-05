"""Shared NSFW classifier — verdicts, failure direction, and recording scope.

The service exists so three consumers with *different* failure tolerances can
share one classification, so the tests that matter most here are the ones
pinning UNKNOWN as distinct from False, and recording as scoped to age-gated
channels only.

Since the Marqo swap there is a second scoping rule with teeth: the NudeNet
tagger runs **only** where rows are recorded, so a body-part inventory can
never be derived from an upload in general chat. Several tests below exist
purely to keep that true.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from bot_modules.services import nsfw_classifier_service as nsfw_module
from bot_modules.core.db_utils import add_config_id, open_db, set_config_value
from bot_modules.services.guess_models import BoundingBox, Detection
from bot_modules.services.nsfw_classifier_service import (
    ACTION_LOGGED,
    ACTION_REMOVED,
    DEFAULT_SFW_THRESHOLD,
    DEFAULT_THRESHOLD,
    IMAGE_EXTENSIONS,
    SFW_MODE_OFF,
    SPOILER_IMAGE_EXTENSIONS,
    SURFACE_SFW,
    SURFACE_SPOILER,
    UNKNOWN,
    Block,
    Classification,
    classifier_for,
    classify_attachment,
    clear_cache,
    evaluate,
    is_age_gated_channel,
    is_classifiable,
    load_settings,
    load_sfw_policy,
    model_name,
    record_block,
    record_block_safely,
    record_classification,
    top_detection,
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


class _FakeNsfwChannel:
    def __init__(self, nsfw: bool) -> None:
        self._nsfw = nsfw

    def is_nsfw(self) -> bool:
        return self._nsfw


class _RaisingChannel:
    def is_nsfw(self) -> bool:
        raise RuntimeError("partial channel object")


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def patched_score(monkeypatch):
    """Replace Marqo inference with a canned probability."""

    def _install(score: float | Exception):
        def fake(_raw: bytes) -> float:
            if isinstance(score, Exception):
                raise score
            return score

        monkeypatch.setattr(
            "bot_modules.services.nsfw_classifier_service._score", fake
        )

    return _install


@pytest.fixture
def patched_tag(monkeypatch):
    """Replace NudeNet tagging with a canned detection list.

    Counts calls, because "the tagger did not run here" is the assertion
    several of these tests are actually making.
    """
    calls: list[bytes] = []

    def _install(detections: list[Detection] | Exception):
        def fake(raw: bytes) -> list[Detection]:
            calls.append(raw)
            if isinstance(detections, Exception):
                raise detections
            return detections

        monkeypatch.setattr("bot_modules.services.nsfw_classifier_service._tag", fake)
        return calls

    return _install


# --------------------------------------------------------------------------
# evaluate() — the pure verdict
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        pytest.param(0.49, False, id="below-threshold"),
        pytest.param(0.5, True, id="at-threshold-inclusive"),
        pytest.param(0.51, True, id="above-threshold"),
        pytest.param(0.0, False, id="floor"),
        pytest.param(1.0, True, id="ceiling"),
    ],
)
def test_evaluate_threshold_boundary(score, expected):
    assert evaluate(score) is expected


def test_evaluate_honors_a_stricter_threshold():
    # The SFW consumer runs the same score at a higher bar and must get the
    # opposite answer — this is the knob that keeps innocent photos alive.
    assert evaluate(0.6, threshold=DEFAULT_THRESHOLD) is True
    assert evaluate(0.6, threshold=DEFAULT_SFW_THRESHOLD) is False


# --------------------------------------------------------------------------
# top_detection() — descriptive tags, never a gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "qualifies"),
    [
        pytest.param("FEMALE_BREAST_EXPOSED", True, id="exposed-qualifies"),
        pytest.param("MALE_GENITALIA_EXPOSED", True, id="genitalia-qualifies"),
        pytest.param("ANUS_EXPOSED", True, id="anus-qualifies"),
        pytest.param("BUTTOCKS_EXPOSED", True, id="buttocks-qualifies"),
        pytest.param("SEX_ACT", True, id="sex-act-qualifies"),
        pytest.param("FEMALE_BREAST_COVERED", False, id="covered-does-not"),
        pytest.param("BUTTOCKS_COVERED", False, id="covered-buttocks-does-not"),
        pytest.param("FEMALE_GENITALIA_COVERED", False, id="covered-gen-does-not"),
        pytest.param("BELLY_EXPOSED", False, id="belly-does-not"),
        pytest.param("ARMPITS_EXPOSED", False, id="armpits-does-not"),
        pytest.param("FACE_FEMALE", False, id="face-does-not"),
    ],
)
def test_top_detection_label_membership(label, qualifies):
    top_label, _ = top_detection([det(label, 0.9)])
    assert (top_label == label) is qualifies


def test_top_detection_reports_highest_scoring_qualifying_label():
    # A very confident non-qualifying detection must never be reported as the
    # headline tag for an image.
    label, score = top_detection([
        det("BELLY_EXPOSED", 0.99),
        det("FEMALE_BREAST_EXPOSED", 0.6),
        det("ANUS_EXPOSED", 0.8),
    ])
    assert label == "ANUS_EXPOSED"
    assert score == 0.8


def test_top_detection_with_nothing_qualifying():
    assert top_detection([]) == (None, None)
    assert top_detection([det("BELLY_EXPOSED", 0.99)]) == (None, None)


def test_top_detection_ignores_the_verdict_threshold():
    # The configured threshold is a whole-image probability. Applying it to a
    # per-part confidence would drop tags for no defensible reason.
    label, score = top_detection([det("SEX_ACT", 0.05)])
    assert (label, score) == ("SEX_ACT", 0.05)


# --------------------------------------------------------------------------
# model_name — a row must name the weights that produced it
# --------------------------------------------------------------------------


def test_model_name_without_tags_is_the_verdict_engine_alone():
    assert model_name(tagged=False) == "marqo-384"


def test_model_name_reports_the_loaded_tagger(monkeypatch):
    # Which NudeNet file wins depends on what is on disk, so it is read back
    # rather than assumed — the old hardcoded "320n" went wrong the moment
    # 640m.onnx was staged.
    monkeypatch.setattr(
        "bot_modules.services.guess_nudenet.active_model_name", lambda: "640m"
    )
    assert model_name(tagged=True) == "marqo-384+640m"


def test_model_name_omits_an_unloaded_tagger(monkeypatch):
    monkeypatch.setattr(
        "bot_modules.services.guess_nudenet.active_model_name", lambda: None
    )
    assert model_name(tagged=True) == "marqo-384"


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------


def test_load_settings_defaults(sync_db_path):
    threshold, sfw_threshold = load_settings(sync_db_path, GUILD)
    assert threshold == DEFAULT_THRESHOLD
    assert sfw_threshold == DEFAULT_SFW_THRESHOLD


def test_load_settings_reads_configured_values(sync_db_path):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, "nsfw_classifier_threshold", "0.4", GUILD)
        set_config_value(conn, "nsfw_classifier_sfw_threshold", "0.9", GUILD)

    threshold, sfw_threshold = load_settings(sync_db_path, GUILD)
    assert threshold == 0.4
    assert sfw_threshold == 0.9


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

    threshold, _ = load_settings(sync_db_path, GUILD)
    assert threshold == DEFAULT_THRESHOLD


def test_sfw_policy_defaults_to_off(sync_db_path):
    # The only content-destroying consumer ships inert; enabling it is a
    # dashboard choice, not a side effect of deploying.
    policy = load_sfw_policy(sync_db_path, GUILD)
    assert policy.mode == SFW_MODE_OFF
    assert policy.is_active is False
    assert policy.deletes is False


@pytest.mark.parametrize(
    ("mode", "active", "deletes"),
    [
        pytest.param("off", False, False, id="off"),
        pytest.param("log", True, False, id="log-reports-only"),
        pytest.param("enforce", True, True, id="enforce-deletes"),
        pytest.param("ENFORCE", True, True, id="case-insensitive"),
        pytest.param("  log  ", True, False, id="whitespace-tolerated"),
        pytest.param("delete-everything", False, False, id="unknown-falls-back-to-off"),
        pytest.param("", False, False, id="empty-falls-back-to-off"),
    ],
)
def test_sfw_policy_modes(sync_db_path, mode, active, deletes):
    # An unrecognised mode must never be guessed at — guessing wrong here
    # deletes members' photos.
    with open_db(sync_db_path) as conn:
        set_config_value(conn, "nsfw_sfw_prevention_mode", mode, GUILD)

    policy = load_sfw_policy(sync_db_path, GUILD)
    assert policy.is_active is active
    assert policy.deletes is deletes


def test_sfw_policy_reads_log_channel_and_exemptions(sync_db_path):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, "nsfw_sfw_prevention_mode", "enforce", GUILD)
        set_config_value(conn, "nsfw_sfw_prevention_log_channel_id", "777", GUILD)
        add_config_id(conn, "nsfw_prevention_exempt_channels", 111, GUILD)
        add_config_id(conn, "nsfw_prevention_exempt_channels", 222, GUILD)

    policy = load_sfw_policy(sync_db_path, GUILD)
    assert policy.log_channel_id == 777
    assert policy.exempt_channel_ids == frozenset({111, 222})


def test_sfw_policy_survives_a_junk_log_channel(sync_db_path):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, "nsfw_sfw_prevention_mode", "enforce", GUILD)
        set_config_value(conn, "nsfw_sfw_prevention_log_channel_id", "not-an-id", GUILD)

    policy = load_sfw_policy(sync_db_path, GUILD)
    assert policy.log_channel_id == 0
    assert policy.deletes is True  # a bad log target doesn't disable the rule


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


def test_spoiler_extensions_are_a_strict_subset():
    # Spoiler enforcement deletes what it matches — including on an unreadable
    # image — so its list is narrower than the classifier's on purpose. Adding
    # a format to IMAGE_EXTENSIONS must not silently make it deletable, and
    # collapsing the two must not happen as a "tidy-up": both directions fail
    # here rather than in production.
    assert set(SPOILER_IMAGE_EXTENSIONS) < set(IMAGE_EXTENSIONS)
    assert ".bmp" not in SPOILER_IMAGE_EXTENSIONS
    assert ".tiff" not in SPOILER_IMAGE_EXTENSIONS


# --------------------------------------------------------------------------
# classify_attachment — failure direction and caching
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classify_returns_verdict_and_metrics(patched_score):
    patched_score(0.88)
    att = FakeAttachment(data=b"x" * 100)

    result = await classify_attachment(att)

    assert result.verdict is True
    assert result.score == 0.88
    assert result.size_bytes == 100
    assert result.threshold == DEFAULT_THRESHOLD
    assert result.inference_ms >= 0


@pytest.mark.asyncio
async def test_classify_does_not_tag(patched_score, patched_tag):
    # The verdict path never runs the tagger — it is scoped to recording, and
    # classify_attachment has no idea whether the channel is age-gated.
    patched_score(0.9)
    calls = patched_tag([det("SEX_ACT", 0.9)])

    result = await classify_attachment(FakeAttachment())

    assert result.verdict is True
    assert result.top_label is None
    assert calls == []


@pytest.mark.asyncio
async def test_classify_non_explicit_is_false_not_unknown(patched_score):
    # "We looked, it isn't explicit" must be distinguishable from "we couldn't
    # tell" — the three consumers branch differently on the two.
    patched_score(0.02)

    result = await classify_attachment(FakeAttachment())

    assert result.verdict is False
    assert result.is_unknown is False


@pytest.mark.asyncio
async def test_download_failure_is_unknown(patched_score):
    patched_score(0.99)
    att = FakeAttachment(error=RuntimeError("cdn is having a day"))

    result = await classify_attachment(att)

    assert result.verdict is UNKNOWN
    assert result.is_unknown is True
    assert result.score is None


@pytest.mark.asyncio
async def test_inference_failure_is_unknown(patched_score):
    patched_score(ValueError("undecodable image"))

    result = await classify_attachment(FakeAttachment())

    assert result.verdict is UNKNOWN


@pytest.mark.asyncio
async def test_missing_model_is_unknown_not_not_explicit(patched_score):
    # A checkout or deploy without the weights must degrade to "we could not
    # tell", never to "we looked and it was clean".
    patched_score(FileNotFoundError("marqo_nsfw_384.onnx"))

    result = await classify_attachment(FakeAttachment())

    assert result.verdict is UNKNOWN


@pytest.mark.asyncio
async def test_unclassifiable_attachment_is_unknown_without_downloading(
    patched_score,
):
    patched_score(0.99)
    att = FakeAttachment(filename="notes.txt", content_type="text/plain")

    result = await classify_attachment(att)

    assert result.verdict is UNKNOWN
    assert att.reads == 0


@pytest.mark.asyncio
async def test_download_timeout_is_unknown(patched_score, monkeypatch):
    monkeypatch.setattr(
        "bot_modules.services.nsfw_classifier_service.DOWNLOAD_TIMEOUT_SECONDS",
        0.01,
    )
    patched_score(0.99)

    result = await classify_attachment(FakeAttachment(delay=0.2))

    assert result.verdict is UNKNOWN


@pytest.mark.asyncio
async def test_second_consumer_reuses_the_cached_verdict(patched_score):
    # Spoiler enforcement and auto-react both fire on the same on_message;
    # the image must be downloaded and classified once between them.
    patched_score(0.9)
    att = FakeAttachment()

    first = await classify_attachment(att)
    second = await classify_attachment(att)

    assert first.verdict is True
    assert second.verdict is True
    assert att.reads == 1


@pytest.mark.asyncio
async def test_a_stricter_threshold_reuses_the_download_but_not_the_verdict(
    patched_score,
):
    # The SFW consumer runs a stricter bar over the same image. It must get its
    # own verdict, but must not pay for a second download and inference — what's
    # cached is the score, which doesn't depend on the threshold.
    patched_score(0.6)
    att = FakeAttachment()

    permissive = await classify_attachment(att, threshold=0.5)
    strict = await classify_attachment(att, threshold=0.9)

    assert permissive.verdict is True
    assert strict.verdict is False
    assert att.reads == 1


@pytest.mark.asyncio
async def test_concurrent_consumers_share_one_download(patched_score):
    # discord.py dispatches each cog's listener as its own task, so the
    # consumers reach the classifier concurrently rather than in sequence.
    # Without in-flight sharing they'd each start their own download.
    patched_score(0.9)
    att = FakeAttachment(delay=0.05)

    results = await asyncio.gather(
        classify_attachment(att),
        classify_attachment(att),
        classify_attachment(att, threshold=0.9),
    )

    assert all(r.verdict is True for r in results)
    assert att.reads == 1


@pytest.mark.asyncio
async def test_unknown_results_are_not_cached(patched_score):
    # A transient CDN failure must not pin UNKNOWN for the life of the process.
    att = FakeAttachment(error=RuntimeError("transient"))
    patched_score(0.9)

    assert (await classify_attachment(att)).is_unknown is True

    att._error = None
    assert (await classify_attachment(att)).verdict is True


# --------------------------------------------------------------------------
# recording — scoped to age-gated channels
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("channel", "expected"),
    [
        pytest.param(_FakeNsfwChannel(True), True, id="age-gated"),
        pytest.param(_FakeNsfwChannel(False), False, id="not-age-gated"),
        pytest.param(object(), False, id="no-is_nsfw-attribute"),
        pytest.param(_RaisingChannel(), False, id="partial-object-raises"),
    ],
)
def test_is_age_gated_channel(channel, expected):
    # Defaults to False on anything unexpected: no dataset recorded and no tip
    # offered when the age gate can't be established.
    assert is_age_gated_channel(channel) is expected


def _classification(**overrides) -> Classification:
    base = dict(
        attachment_id=42,
        verdict=True,
        score=0.91,
        top_label="SEX_ACT",
        top_score=0.77,
        detections=[det("SEX_ACT", 0.77), det("BELLY_EXPOSED", 0.3)],
        tagged=True,
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
    assert row["inference_ms"] == 74
    assert row["bytes"] == 2048
    assert row["created_at"] == 1700000000
    # The threshold is stored per row so the data stays readable after a retune.
    assert row["threshold"] == DEFAULT_THRESHOLD
    # Two engines, two numbers: the verdict came from the score, the tag did
    # not. Conflating them would make the recorded data uninterpretable.
    assert row["marqo_score"] == 0.91
    assert row["top_label"] == "SEX_ACT"
    assert row["top_score"] == 0.77
    # Near-misses are kept — a threshold sweep needs the ones that didn't
    # qualify, not just the ones that did.
    assert [d["label"] for d in detections] == ["BELLY_EXPOSED", "SEX_ACT"]


def test_record_leaves_label_set_empty(sync_db_path):
    # No configurable label set governs a verdict any more; writing the
    # tagger's vocabulary here would imply one did.
    with open_db(sync_db_path) as conn:
        record_classification(
            conn,
            _classification(),
            guild_id=GUILD,
            channel_id=CHANNEL,
            message_id=MESSAGE,
        )
        row = conn.execute(
            "SELECT label_set FROM nsfw_classifications WHERE message_id=?", (MESSAGE,)
        ).fetchone()

    assert row["label_set"] == ""


def test_record_names_both_engines(sync_db_path, monkeypatch):
    monkeypatch.setattr(
        "bot_modules.services.guess_nudenet.active_model_name", lambda: "640m"
    )
    with open_db(sync_db_path) as conn:
        record_classification(
            conn,
            _classification(),
            guild_id=GUILD,
            channel_id=CHANNEL,
            message_id=MESSAGE,
        )
        row = conn.execute(
            "SELECT model FROM nsfw_classifications WHERE message_id=?", (MESSAGE,)
        ).fetchone()

    assert row["model"] == "marqo-384+640m"


def test_record_names_only_marqo_when_the_tagger_never_ran(sync_db_path):
    # A SFW-channel classification, had one ever been recorded: no tagger, so
    # nothing to name beyond the verdict engine.
    with open_db(sync_db_path) as conn:
        record_classification(
            conn,
            _classification(
                detections=[], tagged=False, top_label=None, top_score=None
            ),
            guild_id=GUILD,
            channel_id=CHANNEL,
            message_id=MESSAGE,
        )
        row = conn.execute(
            "SELECT model FROM nsfw_classifications WHERE message_id=?", (MESSAGE,)
        ).fetchone()

    assert row["model"] == "marqo-384"


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
            _classification(verdict=UNKNOWN, score=None, top_label=None, top_score=None),
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


# --------------------------------------------------------------------------
# blocks — the record of what was destroyed
# --------------------------------------------------------------------------


def _block(**overrides) -> Block:
    base = dict(
        message_id=MESSAGE,
        attachment_id=42,
        guild_id=GUILD,
        channel_id=CHANNEL,
        author_id=999,
        filename="holiday.jpg",
        score=0.93,
        surface=SURFACE_SFW,
        action=ACTION_REMOVED,
    )
    base.update(overrides)
    return Block(**base)


def test_record_block_writes_a_row(sync_db_path):
    with open_db(sync_db_path) as conn:
        record_block(conn, _block(), now=1700000000)
        row = conn.execute("SELECT * FROM nsfw_blocks").fetchone()

    assert row["author_id"] == 999
    assert row["filename"] == "holiday.jpg"
    assert row["marqo_score"] == 0.93
    assert row["surface"] == SURFACE_SFW
    assert row["action"] == ACTION_REMOVED
    assert row["created_at"] == 1700000000


def test_record_block_keeps_an_unreadable_image_distinct_from_a_low_score(
    sync_db_path,
):
    # Spoiler enforcement deletes on an unreadable image by design. A row
    # claiming 0.0 would read as "the model was sure it was clean, and we
    # deleted it anyway" — the opposite of what happened.
    with open_db(sync_db_path) as conn:
        record_block(conn, _block(score=None, surface=SURFACE_SPOILER))
        row = conn.execute("SELECT * FROM nsfw_blocks").fetchone()

    assert row["marqo_score"] is None


def test_record_block_stores_log_mode_separately(sync_db_path):
    with open_db(sync_db_path) as conn:
        record_block(conn, _block(action=ACTION_LOGGED))
        row = conn.execute("SELECT action FROM nsfw_blocks").fetchone()

    assert row["action"] == ACTION_LOGGED


def test_record_block_is_idempotent_per_attachment(sync_db_path):
    with open_db(sync_db_path) as conn:
        record_block(conn, _block())
        record_block(conn, _block())
        count = conn.execute("SELECT COUNT(*) c FROM nsfw_blocks").fetchone()["c"]

    assert count == 1


def test_record_block_safely_swallows_storage_failures(tmp_path):
    # The audit trail failing must never change what happened to the member,
    # and this runs on the enforcement path itself.
    record_block_safely(tmp_path / "nonexistent" / "no.db", _block())


# --------------------------------------------------------------------------
# classifier_for — the shared consumer entry point
# --------------------------------------------------------------------------


def _rows(db_path) -> int:
    with open_db(db_path) as conn:
        return conn.execute("SELECT COUNT(*) c FROM nsfw_classifications").fetchone()[
            "c"
        ]


class FakeMessage:
    def __init__(self, *, nsfw: bool) -> None:
        self.id = MESSAGE
        self.guild = type("G", (), {"id": GUILD})()
        self.channel = _FakeNsfwChannel(nsfw)
        self.channel.id = CHANNEL


@pytest.mark.asyncio
async def test_classifier_records_and_tags_in_an_age_gated_channel(
    sync_db_path, patched_score, patched_tag
):
    patched_score(0.9)
    calls = patched_tag([det("SEX_ACT", 0.8)])
    classify = classifier_for(sync_db_path, FakeMessage(nsfw=True))

    result = await classify(FakeAttachment())

    assert result.verdict is True
    assert result.top_label == "SEX_ACT"
    assert len(calls) == 1
    assert _rows(sync_db_path) == 1


@pytest.mark.asyncio
async def test_classifier_does_not_record_or_tag_in_a_sfw_channel(
    sync_db_path, patched_score, patched_tag
):
    # Classification still happens — SFW prevention needs the verdict — but no
    # dataset is built out of general chat, and the tagger never even runs, so
    # no body-part inventory of the image exists to leak.
    patched_score(0.9)
    calls = patched_tag([det("SEX_ACT", 0.8)])
    classify = classifier_for(sync_db_path, FakeMessage(nsfw=False))

    result = await classify(FakeAttachment())

    assert result.verdict is True
    assert result.top_label is None
    assert calls == []
    assert _rows(sync_db_path) == 0


@pytest.mark.asyncio
async def test_unknown_verdicts_are_never_tagged(
    sync_db_path, patched_score, patched_tag
):
    # No verdict means nothing to describe, and running the tagger anyway would
    # burn inference on an image the service has already given up on.
    patched_score(RuntimeError("model exploded"))
    calls = patched_tag([det("SEX_ACT", 0.8)])

    result = await classifier_for(sync_db_path, FakeMessage(nsfw=True))(
        FakeAttachment()
    )

    assert result.is_unknown is True
    assert calls == []
    assert _rows(sync_db_path) == 0


@pytest.mark.asyncio
async def test_a_tagging_failure_still_records_the_verdict(
    sync_db_path, patched_score, patched_tag
):
    # Tags are metrics; the verdict is already made and is what consumers act
    # on. A tagger failure leaves the row unlabelled, not unwritten.
    patched_score(0.9)
    patched_tag(RuntimeError("nudenet is unhappy"))

    result = await classifier_for(sync_db_path, FakeMessage(nsfw=True))(
        FakeAttachment()
    )

    assert result.verdict is True
    assert result.top_label is None
    assert _rows(sync_db_path) == 1


@pytest.mark.asyncio
async def test_two_consumers_share_one_tagging_pass(
    sync_db_path, patched_score, patched_tag
):
    # Tipping and spoiler enforcement both fire in an age-gated channel. Without
    # a shared cache they would each pay for their own NudeNet run.
    patched_score(0.9)
    calls = patched_tag([det("SEX_ACT", 0.8)])
    att = FakeAttachment()
    message = FakeMessage(nsfw=True)

    await classifier_for(sync_db_path, message)(att)
    await classifier_for(sync_db_path, message)(att)

    assert len(calls) == 1
    assert att.reads == 1


@pytest.mark.asyncio
async def test_classifier_derives_the_age_gate_itself(sync_db_path):
    # The recording-scope flag is derived from the channel, not supplied by
    # the caller — a caller can no longer assert a precondition that a guard
    # in another module is responsible for.
    assert classifier_for(sync_db_path, FakeMessage(nsfw=True)).channel_is_nsfw is True
    assert classifier_for(sync_db_path, FakeMessage(nsfw=False)).channel_is_nsfw is False


@pytest.mark.asyncio
async def test_strict_classifier_applies_the_higher_threshold(
    sync_db_path, patched_score
):
    # Same image, same score: permissive says explicit, strict does not.
    patched_score(0.6)
    att = FakeAttachment()
    message = FakeMessage(nsfw=False)

    permissive = await classifier_for(sync_db_path, message)(att)
    strict = await classifier_for(sync_db_path, message, strict=True)(att)

    assert permissive.verdict is True
    assert strict.verdict is False
    assert strict.threshold == DEFAULT_SFW_THRESHOLD


@pytest.mark.asyncio
async def test_settings_are_read_once_per_message_not_per_attachment(
    sync_db_path, patched_score, monkeypatch
):
    patched_score(0.9)
    calls = []
    real = nsfw_module.load_settings
    monkeypatch.setattr(
        nsfw_module,
        "load_settings",
        lambda *a, **k: (calls.append(1), real(*a, **k))[1],
    )

    classify = classifier_for(sync_db_path, FakeMessage(nsfw=False))
    # Gathered, not sequential. The auto-react cog fans classify() out across a
    # message's attachments, so every coroutine reaches the lazy load in the
    # same tick — which a plain cached value does NOT survive: each would see
    # None and open its own connection. Awaiting these in a loop passes either
    # way and so pins nothing.
    await asyncio.gather(
        *(classify(FakeAttachment(attachment_id=i)) for i in range(3))
    )

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_classifier_returns_a_verdict_when_recording_fails(
    sync_db_path, patched_score, patched_tag, monkeypatch
):
    # Metrics are a side effect; a write failure must not change what the
    # consumer does about the image.
    patched_score(0.9)
    patched_tag([det("SEX_ACT", 0.8)])

    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(nsfw_module, "record_classification", boom)

    result = await classifier_for(sync_db_path, FakeMessage(nsfw=True))(
        FakeAttachment()
    )

    assert result.verdict is True


@pytest.mark.asyncio
async def test_an_image_the_tagger_saw_nothing_in_still_names_the_tagger(
    sync_db_path, patched_score, patched_tag, monkeypatch
):
    # The blind-spot case: explicit verdict, tagger ran, tagger found nothing.
    # If the row named Marqo alone it would be indistinguishable from one that
    # was never tagged, and the disagreement count the report is built on would
    # be unauditable.
    monkeypatch.setattr(
        "bot_modules.services.guess_nudenet.active_model_name", lambda: "640m"
    )
    patched_score(0.91)
    patched_tag([])

    await classifier_for(sync_db_path, FakeMessage(nsfw=True))(FakeAttachment())

    with open_db(sync_db_path) as conn:
        row = conn.execute(
            "SELECT model, top_label FROM nsfw_classifications"
        ).fetchone()

    assert row["model"] == "marqo-384+640m"
    assert row["top_label"] is None
