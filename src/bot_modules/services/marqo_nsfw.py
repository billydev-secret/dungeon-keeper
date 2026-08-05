"""Marqo NSFW image classifier — the verdict engine behind the moderation gates.

A whole-image classifier: one probability that an image is explicit. It has no
localization, which is the reason the Guess pipeline still runs NudeNet — see
``guess_nudenet`` — and the reason this module answers "is it?" and never
"where?".

It replaced NudeNet as the *verdict* engine because NudeNet could not see the
content the moderation gates exist to catch — see docs/nsfw_classifier_spec.md
for the measurements behind that decision.

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
        width, height = image.size
        # Crop to the central square first, then resize once — so every resize
        # outputs exactly INPUT_SIZE**2. timm expresses this as Resize(shortest
        # edge) then CenterCrop, which selects the same square; doing it in that
        # order instead builds an intermediate of (long / short) * 384 x 384 px,
        # unbounded because MAX_IMAGE_BYTES caps encoded bytes and not
        # dimensions. See docs/nsfw_classifier_spec.md §Preprocessing, and
        # test_preprocess_never_builds_a_large_intermediate, which pins it.
        side = min(width, height)
        left = (width - side) // 2
        top = (height - side) // 2
        # Crop before convert("RGB"), and skip the convert entirely when the
        # mode already matches: PIL's convert is `if mode == self.mode: return
        # self.copy()`, a full-size buffer copy for no gain — and RGB is what
        # the common JPEG upload already is. Between them these avoid two
        # multi-MB allocations per image (27 MB each on a 4000x3000 photo).
        # Cropping first needs only image.size, so the bound argued above is
        # unchanged.
        square = image.crop((left, top, left + side, top + side))
        if square.mode != "RGB":
            square = square.convert("RGB")
        resized = square.resize((INPUT_SIZE, INPUT_SIZE), Image.Resampling.BICUBIC)
        # np.array rather than asarray to say plainly that we own this buffer,
        # since the normalization below mutates it. (PIL hands numpy a fresh
        # copy either way, so this costs nothing.)
        pixels = np.array(resized, dtype=np.float32)

    # timm's mean=0.5, std=0.5 folded: (x / 255 - 0.5) / 0.5 is x / 127.5 - 1.
    # Applied in place, that is one allocation and two passes, where the
    # unfolded form allocated three 1.7 MB intermediates per image. Named
    # constants would label the 127.5 and leave the 1.0 bare, which is worse
    # than one line of arithmetic with the derivation above it.
    pixels *= 1.0 / 127.5
    pixels -= 1.0
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
