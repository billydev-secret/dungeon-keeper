"""Marqo NSFW classifier — preprocessing, softmax, and lazy single-load.

The weights are gitignored and deployed to disk, so nothing here runs real
inference. What is tested is everything around it: the transform (which is
where a silent accuracy regression would hide), the label-index convention,
and the lazy-load contract three concurrent consumers depend on.

The preprocessing assertions are the load-bearing ones. Squashing the image to
a square instead of resizing-and-cropping scored the reference image 0.879
where the real transform scores 0.912 — a change nothing else in the suite
would notice.
"""
from __future__ import annotations

import io
import threading

import numpy as np
import pytest
from PIL import Image

from bot_modules.services import marqo_nsfw
from bot_modules.services.marqo_nsfw import (
    INPUT_SIZE,
    NSFW_INDEX,
    preprocess,
    score_bytes,
)


def png(width: int, height: int, color=(128, 64, 32)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _reset_session():
    marqo_nsfw.reset_session()
    yield
    marqo_nsfw.reset_session()


class FakeSession:
    """Stands in for onnxruntime.InferenceSession."""

    def __init__(self, logits=(2.0, 0.0)) -> None:
        self.logits = logits
        self.calls: list[np.ndarray] = []

    def run(self, _outputs, feeds):
        self.calls.append(feeds["pixels"])
        return [np.array([list(self.logits)], dtype=np.float32)]


# --------------------------------------------------------------------------
# preprocess — the transform
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("width", "height"),
    [
        pytest.param(384, 384, id="already-square"),
        pytest.param(1536, 1024, id="landscape"),
        pytest.param(1024, 1536, id="portrait"),
        pytest.param(64, 64, id="smaller-than-input"),
        pytest.param(4000, 100, id="extreme-landscape"),
        pytest.param(100, 4000, id="extreme-portrait"),
        # Under the resize-first order these built a ~295 Mpx intermediate
        # (~0.9 GB) from a 114-byte file — see test_preprocess_never_builds_a
        # _large_intermediate below.
        pytest.param(4000, 2, id="degenerate-landscape"),
        pytest.param(2, 4000, id="degenerate-portrait"),
    ],
)
def test_preprocess_always_produces_the_model_input_shape(width, height):
    # An extreme aspect ratio can round the long edge down to 383 and make the
    # centre crop run off the image, which would raise rather than classify.
    assert preprocess(png(width, height)).shape == (1, 3, INPUT_SIZE, INPUT_SIZE)


def test_preprocess_never_builds_a_large_intermediate(monkeypatch):
    """Every resize must target the input size exactly.

    MAX_IMAGE_BYTES bounds encoded bytes, not dimensions, so a tiny file can
    carry an extreme aspect ratio. Resizing the shortest edge to 384 first
    made an intermediate of (long / short) * 384 x 384: a 114-byte 4000x2 PNG
    became 295 Mpx / ~0.9 GB / 2.5 s, and a handful posted together would OOM
    the bot. Asserting on the requested size pins the invariant directly,
    where a timing or RSS assertion would be flaky.
    """
    seen = []
    original = Image.Image.resize

    def spy(self, size, *args, **kwargs):
        seen.append(size)
        return original(self, size, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "resize", spy)
    preprocess(png(4000, 2))
    preprocess(png(2, 4000))
    preprocess(png(1536, 1024))

    assert seen, "no resize happened — the spy is not wired up"
    assert all(s == (INPUT_SIZE, INPUT_SIZE) for s in seen), seen


def test_preprocess_normalizes_to_signed_unit_range():
    white = preprocess(png(400, 400, (255, 255, 255)))
    black = preprocess(png(400, 400, (0, 0, 0)))
    mid = preprocess(png(400, 400, (128, 128, 128)))

    assert np.allclose(white, 1.0)
    assert np.allclose(black, -1.0)
    # atol against a float64 expectation: the tensor is float32, and the
    # midpoint value is small enough that the default rtol is tighter than
    # single precision can honour.
    assert np.allclose(mid, (128 / 255 - 0.5) / 0.5, atol=1e-6)


def test_preprocess_is_channel_first_rgb():
    # A CHW/HWC mix-up or a BGR swap still produces a valid-shaped tensor, so
    # only a per-channel assertion catches it.
    pixels = preprocess(png(400, 400, (255, 0, 0)))

    assert pixels.shape[1] == 3
    assert np.allclose(pixels[0, 0], 1.0)  # R
    assert np.allclose(pixels[0, 1], -1.0)  # G
    assert np.allclose(pixels[0, 2], -1.0)  # B


def test_preprocess_is_float32():
    # onnxruntime rejects a float64 feed for a tensor(float) input.
    assert preprocess(png(400, 400)).dtype == np.float32


def test_preprocess_centre_crops_rather_than_squashing():
    # A wide image is resized on its shortest edge and cropped, so the centre
    # survives and the far edges do not. Squashing would keep both edges and
    # score differently — that is the 0.879-vs-0.912 gap.
    image = Image.new("RGB", (1200, 400), (0, 0, 0))
    image.paste(Image.new("RGB", (200, 400), (255, 255, 255)), (500, 0))  # centre
    image.paste(Image.new("RGB", (100, 400), (255, 0, 0)), (0, 0))  # far left
    buf = io.BytesIO()
    image.save(buf, format="PNG")

    pixels = preprocess(buf.getvalue())

    # The white centre band survives the crop.
    assert pixels[0, :, INPUT_SIZE // 2, INPUT_SIZE // 2].min() > 0.9
    # The red far-left band is outside it entirely — no pixel is red-dominant.
    red_dominant = (pixels[0, 0] > 0.5) & (pixels[0, 1] < -0.5)
    assert not red_dominant.any()


def test_preprocess_handles_paletted_and_greyscale_images():
    # Discord serves plenty of both; convert() has to give every path three
    # channels or the feed shape is wrong.
    buf = io.BytesIO()
    Image.new("L", (400, 400), 200).save(buf, format="PNG")
    assert preprocess(buf.getvalue()).shape == (1, 3, INPUT_SIZE, INPUT_SIZE)

    buf = io.BytesIO()
    Image.new("P", (400, 400)).save(buf, format="PNG")
    assert preprocess(buf.getvalue()).shape == (1, 3, INPUT_SIZE, INPUT_SIZE)


def test_preprocess_rejects_undecodable_bytes():
    # The classifier service turns this into UNKNOWN rather than a verdict.
    with pytest.raises(Exception):
        preprocess(b"this is not an image")


# --------------------------------------------------------------------------
# score_bytes — softmax and label order
# --------------------------------------------------------------------------


def test_score_reads_the_nsfw_index(monkeypatch):
    # label_names on the source model is ['NSFW', 'SFW']; reading index 1 would
    # invert every verdict the bot makes while still looking like a probability.
    assert NSFW_INDEX == 0
    session = FakeSession(logits=(2.0, 0.0))
    monkeypatch.setattr(marqo_nsfw, "_get_session", lambda: session)

    high = score_bytes(png(400, 400))

    session.logits = (0.0, 2.0)
    low = score_bytes(png(400, 400))

    assert high > 0.8
    assert low < 0.2


def test_score_is_a_probability(monkeypatch):
    monkeypatch.setattr(marqo_nsfw, "_get_session", lambda: FakeSession((0.5, 0.5)))
    assert score_bytes(png(400, 400)) == pytest.approx(0.5)


def test_score_survives_large_logits(monkeypatch):
    # Shifted softmax: an unshifted exp() overflows to inf/inf = nan here, and
    # a nan score compares False against every threshold, silently disabling
    # the gate rather than erring.
    monkeypatch.setattr(marqo_nsfw, "_get_session", lambda: FakeSession((900.0, 1.0)))
    score = score_bytes(png(400, 400))
    assert score == pytest.approx(1.0)
    assert not np.isnan(score)


def test_score_feeds_the_named_input(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(marqo_nsfw, "_get_session", lambda: session)

    score_bytes(png(400, 400))

    assert session.calls[0].shape == (1, 3, INPUT_SIZE, INPUT_SIZE)


# --------------------------------------------------------------------------
# lazy load — one session, whatever the concurrency
# --------------------------------------------------------------------------


def test_missing_weights_raise_a_named_error(monkeypatch, tmp_path):
    # onnxruntime's own error for an absent .onnx.data sibling is a raw
    # protobuf complaint that names neither the file nor where it was expected.
    monkeypatch.setattr(marqo_nsfw, "model_dir", lambda: tmp_path)

    with pytest.raises(FileNotFoundError, match="marqo_nsfw_384.onnx"):
        marqo_nsfw._get_session()


def test_missing_weights_sibling_raises(monkeypatch, tmp_path):
    # The graph alone is not enough — the exporter split the weights out.
    (tmp_path / "marqo_nsfw_384.onnx").write_bytes(b"graph")
    monkeypatch.setattr(marqo_nsfw, "model_dir", lambda: tmp_path)

    with pytest.raises(FileNotFoundError, match=r"marqo_nsfw_384\.onnx\.data"):
        marqo_nsfw._get_session()


def test_is_available_needs_both_files(monkeypatch, tmp_path):
    monkeypatch.setattr(marqo_nsfw, "model_dir", lambda: tmp_path)
    assert marqo_nsfw.is_available() is False

    (tmp_path / "marqo_nsfw_384.onnx").write_bytes(b"graph")
    assert marqo_nsfw.is_available() is False

    (tmp_path / "marqo_nsfw_384.onnx.data").write_bytes(b"weights")
    assert marqo_nsfw.is_available() is True


def test_concurrent_callers_load_one_session(monkeypatch, tmp_path):
    # Three consumers fire off one on_message and reach inference from separate
    # to_thread workers. Without the lock each builds its own 22 MB session.
    (tmp_path / "marqo_nsfw_384.onnx").write_bytes(b"graph")
    (tmp_path / "marqo_nsfw_384.onnx.data").write_bytes(b"weights")
    monkeypatch.setattr(marqo_nsfw, "model_dir", lambda: tmp_path)

    built = []
    started = threading.Barrier(8)

    class SlowRuntime:
        @staticmethod
        def InferenceSession(_path, providers=None):  # noqa: N802 - mirrors ort
            built.append(1)
            return FakeSession()

    monkeypatch.setitem(__import__("sys").modules, "onnxruntime", SlowRuntime)

    def worker():
        started.wait()
        marqo_nsfw._get_session()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(built) == 1
