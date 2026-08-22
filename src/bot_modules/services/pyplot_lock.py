"""One process-wide lock for every pyplot-based chart renderer.

pyplot keeps a global figure registry (``Gcf``) that is not thread-safe, and
this bot renders charts on ``asyncio.to_thread`` workers from more than one
module — ``activity_graphs`` (``/modinfo``, ``/info``) and ``pools_charts``
(the casino Pools panel). A lock living in either module would serialize that
module against itself and nothing else, which is the failure this file exists
to prevent: a member opening ``/info`` while another refreshes the Pools panel
puts two workers into the same global registry at once.

Reentrant on purpose: ``render_nsfw_gender_line_chart`` delegates to
``render_nsfw_gender_chart`` for its single-bucket case, so a plain ``Lock``
would deadlock the worker the first time that path ran.

Rendering is ~100ms, so the queue is not a bottleneck — and interleaved access
to that registry is not a slow chart, it is the wrong chart or a crash.
"""

from __future__ import annotations

import functools
import threading

RENDER_LOCK = threading.RLock()


def serialized_render(fn):
    """Serialize a pyplot-based renderer against every other one.

    A decorator rather than a ``with`` inside each body: the bodies are long
    and each has several early returns, and a lock released on one path but
    not another is worse than no lock at all.

    Apply it to **every** renderer, including ones with no caller today. They
    all mutate the same global registry, so a partially-covered lock protects
    nothing the moment someone wires up one of the others.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with RENDER_LOCK:
            return fn(*args, **kwargs)

    return wrapper
