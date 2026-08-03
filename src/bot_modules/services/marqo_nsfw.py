"""Marqo NSFW image classifier — the verdict engine behind the moderation gates.

A whole-image classifier: one probability that an image is explicit. It has no
localization, which is the reason the Guess pipeline still runs NudeNet — see
``guess_nudenet`` — and the reason this module answers "is it?" and never
"where?".

It replaced NudeNet as the *verdict* engine because NudeNet could not see the
content it exists to catch. On a dark, warm-monochrome boudoir photo that
passed straight through an enforcing SFW gate, NudeNet 320n returned zero
detections (even cropped and brightened) and 640m only a 0.26
``MALE_BREAST_EXPOSED``; this model scores it 0.91, against 0.04–0.08 for
non-explicit control images. That lighting is simply outside NudeNet's
training data.

Weights live in ``models/``, which is gitignored and deployed to disk like the
other model files. A checkout without them imports fine and fails only when a
classification is actually attempted — which the classifier service turns into
``UNKNOWN``, so a missing model degrades to "we could not tell" rather than to
a wrong verdict.

onnxruntime, PIL and numpy are imported lazily for the same reason: importing
this module must stay free on a machine that never classifies anything.
"""
from __future__ import annotations

import io
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

log = logging.getLogger("dungeonkeeper.nsfw")

#: Recorded into ``nsfw_classifications.model`` so a row states which weights
#: produced its verdict.
MODEL_NAME = "marqo-384"

MODEL_FILENAME = "marqo_nsfw_384.onnx"

#: The exporter split the weights out of the graph, so the ``.data`` sibling is
#: as required as the ``.onnx`` itself — onnxruntime resolves it by name,
#: relative to the graph file.
WEIGHTS_FILENAME = MODEL_FILENAME + ".data"

#: Square input the model was trained at.
INPUT_SIZE = 384

#: ``label_names`` on the source model is ``['NSFW', 'SFW']``, so index 0 of
#: the softmaxed logits is the probability we want.
NSFW_INDEX = 0

#: timm's eval transform for this model uses ``crop_pct=1.0``: the shortest
#: edge is resized to the input size and a centre crop is taken. Squashing the
#: image to a square instead is close but not equal — it scored the reference
#: image 0.879 where the real transform scores 0.912 — so this matches timm.
#: The cost is a genuine blind spot: content at the far edge of a very wide or
#: very tall image falls outside the crop and is never seen.
_normalize_mean = 0.5
_normalize_std = 0.5

_session = None
# Three consumers fire off one ``on_message`` and reach inference concurrently
# through ``asyncio.to_thread`` workers. Without the lock two of them can each
# build a session — a second 22 MB model load, and an orphaned session left
# behind. Mirrors guess_nudenet._get_detector.
_session_lock = threading.Lock()


def model_dir() -> Path:
    return Path(__file__).parent.parent / "models"


def is_available() -> bool:
    """Whether the weights are on disk, without loading them."""
    return (model_dir() / MODEL_FILENAME).exists() and (
        model_dir() / WEIGHTS_FILENAME
    ).exists()


def _get_session():  # type: ignore[no-untyped-def]
    global _session
    if _session is not None:
        return _session
    with _session_lock:
        if _session is not None:
            return _session
        import onnxruntime  # noqa: PLC0415

        graph = model_dir() / MODEL_FILENAME
        weights = model_dir() / WEIGHTS_FILENAME
        # Checked separately: onnxruntime's error for an absent .data sibling
        # is a raw protobuf complaint that says nothing about which file is
        # missing or where it was expected.
        for path in (graph, weights):
            if not path.exists():
                raise FileNotFoundError(f"Marqo NSFW model file missing: {path}")
        _session = onnxruntime.InferenceSession(
            str(graph), providers=["CPUExecutionProvider"]
        )
        log.info("loaded Marqo NSFW classifier from %s", graph)
    return _session


def preprocess(raw: bytes) -> np.ndarray:
    """Decode *raw* into the model's input tensor.

    Returns float32 NCHW ``(1, 3, 384, 384)``, RGB, normalized to roughly
    ``[-1, 1]``. Pure apart from the decode, so it is tested directly.
    """
    import numpy as np  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    with Image.open(io.BytesIO(raw)) as image:
        # Animated GIFs and paletted PNGs both land here; convert() takes the
        # first frame and gives every path the same three channels.
        rgb = image.convert("RGB")
        width, height = rgb.size
        # Crop to the central square first, then resize once to the input size.
        #
        # timm's transform is Resize(shortest edge -> 384) then CenterCrop(384),
        # which in source coordinates selects exactly this square — so this is
        # equivalent (measured: within 0.0012 of the resize-first order) while
        # being bounded. Resizing first is not: the intermediate is
        # (long / short) * 384 x 384 px, and the only upstream guard is
        # MAX_IMAGE_BYTES, which bounds *encoded bytes* and says nothing about
        # dimensions. A 114-byte 4000x2 PNG sails through that cap and expands
        # to 295 Mpx (~0.9 GB, 2.5 s); a few of those posted together would
        # take the bot's process out.
        side = min(width, height)
        left = (width - side) // 2
        top = (height - side) // 2
        square = rgb.crop((left, top, left + side, top + side))
        resized = square.resize((INPUT_SIZE, INPUT_SIZE), Image.Resampling.BICUBIC)
        pixels = np.asarray(resized, dtype=np.float32) / 255.0

    pixels = (pixels - _normalize_mean) / _normalize_std
    return pixels.transpose(2, 0, 1)[None]


def score_bytes(raw: bytes) -> float:
    """Probability that *raw* is an explicit image, in ``[0, 1]``.

    Blocking — onnxruntime runs in C++ — so callers run it off the event loop.
    Raises on an undecodable image or a missing model; the classifier service
    turns either into ``UNKNOWN``.
    """
    import numpy as np  # noqa: PLC0415

    logits = _get_session().run(None, {"pixels": preprocess(raw)})[0]
    row = np.asarray(logits[0], dtype=np.float64)
    # Shifted for numerical stability; the model emits raw logits.
    exponentiated = np.exp(row - row.max())
    return float((exponentiated / exponentiated.sum())[NSFW_INDEX])


def reset_session() -> None:
    """Drop the loaded session (tests)."""
    global _session
    _session = None
