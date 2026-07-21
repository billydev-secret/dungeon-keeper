"""Local LLM inference, either in-process (llama-cpp-python) or over HTTP.

Two backends, chosen by whether ``LLAMA_SERVER_URL`` is set:

**in-process** (default) — the GGUF loads (and downloads if needed) in a
background thread at startup so the bot comes online immediately. Any
``chat()`` call that arrives before loading finishes awaits the ready event.
Inference is serialised through a single worker, so one slow call blocks all
AI features.

**remote** — point at a llama.cpp ``llama-server`` on another machine (e.g. one
with a GPU) and calls go out over HTTP instead. Nothing loads locally, and
concurrent calls are handled by the server's own continuous batching rather
than queueing behind a single thread.

The remote endpoint is **restricted to private/loopback addresses** by default
so that moderation content cannot be pointed at a third-party inference API by
a stray config edit; set ``LLAMA_SERVER_ALLOW_PUBLIC=1`` to override
deliberately. See ``is_private_endpoint``.

Config is read from the database first, falling back to environment variables.
DB keys (all guild_id=0):
    llm_model_path   Full path where the GGUF file is stored/expected.
    llm_hf_repo      HuggingFace repo ID  (e.g. bartowski/Llama-3.2-3B-Instruct-GGUF)
    llm_hf_file      Filename within the repo (e.g. Llama-3.2-3B-Instruct-Q4_K_M.gguf)

Env var fallbacks (used when DB keys are absent):
    LLAMA_MODEL_PATH, LLAMA_HF_REPO, LLAMA_HF_FILE
    LLAMA_N_CTX, LLAMA_N_GPU_LAYERS, LLAMA_N_THREADS

Remote backend (env only — this is deployment topology, like LAVALINK_HOST):
    LLAMA_SERVER_URL           Base URL of llama-server, e.g. http://192.168.1.20:8080
    LLAMA_SERVER_TIMEOUT       Per-request timeout in seconds (default 120).
    LLAMA_SERVER_ALLOW_PUBLIC  Set to 1 to permit a non-private endpoint.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

log = logging.getLogger("dungeonkeeper.llm")

_DEFAULT_HF_REPO = "bartowski/Llama-3.2-3B-Instruct-GGUF"
_DEFAULT_HF_FILE = "Llama-3.2-3B-Instruct-Q4_K_M.gguf"
_DEFAULT_MODEL_PATH = "./models/Llama-3.2-3B-Instruct-Q4_K_M.gguf"

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm-inference")

_model = None  # llama_cpp.Llama once loaded
_model_error: str | None = None
_ready_event: asyncio.Event | None = None
_phase: Literal["idle", "downloading", "loading", "ready", "error"] = "idle"


# ── Config helpers ─────────────────────────────────────────────────────────────


def _read_db_config(db_path: Path) -> tuple[str, str, str]:
    """Return (model_path, hf_repo, hf_file) from the DB, empty strings if absent."""
    try:
        from bot_modules.core.db_utils import get_config_value, open_db
        with open_db(db_path) as conn:
            model_path = get_config_value(conn, "llm_model_path", "")
            hf_repo    = get_config_value(conn, "llm_hf_repo", "")
            hf_file    = get_config_value(conn, "llm_hf_file", "")
        return model_path, hf_repo, hf_file
    except Exception as exc:
        log.warning("Could not read LLM config from DB: %s", exc)
        return "", "", ""


def get_config(db_path: Path | None = None) -> tuple[str, str, str]:
    """Return (model_path, hf_repo, hf_file), DB takes precedence over env vars."""
    env_path = os.getenv("LLAMA_MODEL_PATH", "")
    env_repo = os.getenv("LLAMA_HF_REPO", "")
    env_file = os.getenv("LLAMA_HF_FILE", "")

    if db_path is None:
        return (
            env_path or _DEFAULT_MODEL_PATH,
            env_repo or _DEFAULT_HF_REPO,
            env_file or _DEFAULT_HF_FILE,
        )

    db_path_val, db_repo, db_file = _read_db_config(db_path)
    return (
        db_path_val or env_path or _DEFAULT_MODEL_PATH,
        db_repo    or env_repo or _DEFAULT_HF_REPO,
        db_file    or env_file or _DEFAULT_HF_FILE,
    )


# ── Remote backend ─────────────────────────────────────────────────────────────

# Hostnames that are unambiguously LAN-local even though they aren't IP literals.
_LOCAL_HOST_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localdomain")


def is_private_endpoint(url: str) -> bool:
    """True if ``url`` points somewhere that cannot leave the local network.

    Conversation windows sent to the guard model are exactly the content we
    keep off third-party AI services, so the remote backend refuses to talk to
    anything that isn't demonstrably local unless explicitly overridden.

    IP literals are classified by :mod:`ipaddress` (loopback, RFC1918 private,
    link-local, or unique-local v6). Bare hostnames can't be classified without
    a DNS lookup, so only ``localhost`` and an explicit local-suffix allowlist
    pass — an unqualified name resolves to *something*, and we'd rather refuse
    than guess wrong about where content is going.
    """
    host = (urlparse(url).hostname or "").strip().lower()
    if not host:
        return False
    if host == "localhost":
        return True

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host.endswith(_LOCAL_HOST_SUFFIXES)

    return bool(ip.is_loopback or ip.is_private or ip.is_link_local)


def get_server_url() -> str:
    """Return the remote llama-server base URL, or '' when running in-process.

    Returns '' (falling back to the in-process backend) if the configured URL
    is non-private and ``LLAMA_SERVER_ALLOW_PUBLIC`` is not set, logging why.
    """
    url = os.getenv("LLAMA_SERVER_URL", "").strip().rstrip("/")
    if not url:
        return ""

    if not is_private_endpoint(url) and os.getenv("LLAMA_SERVER_ALLOW_PUBLIC", "") not in ("1", "true", "yes"):
        log.error(
            "LLAMA_SERVER_URL=%s is not a private/loopback address — refusing to send "
            "content off-network. Set LLAMA_SERVER_ALLOW_PUBLIC=1 to override. "
            "Falling back to in-process inference.",
            url,
        )
        return ""

    return url


async def _remote_chat(
    url: str,
    *,
    system: str,
    user_content: str,
    max_tokens: int,
    temperature: float,
) -> str:
    """Single-turn chat against llama-server's OpenAI-compatible endpoint."""
    import httpx

    timeout = float(os.getenv("LLAMA_SERVER_TIMEOUT", "120"))
    payload = {
        # llama-server serves one model; the name is advisory. Deliberately not
        # the caller's `model` arg — see chat()'s note on why that stays ignored.
        "model": "local",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{url}/v1/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()

    elapsed = time.monotonic() - t0
    text = (data["choices"][0]["message"]["content"] or "").strip()
    tokens_out = (data.get("usage") or {}).get("completion_tokens", "?")
    log.debug("LLM (remote) response %.1fs %s tokens: %.120s", elapsed, tokens_out, text)
    return text


# ── Public helpers ─────────────────────────────────────────────────────────────


def is_available(db_path: Path | None = None) -> bool:
    if get_server_url():
        return True
    model_path, hf_repo, hf_file = get_config(db_path)
    return bool(model_path or (hf_repo and hf_file))


def default_model(db_path: Path | None = None) -> str:
    url = get_server_url()
    if url:
        return f"llama-server ({urlparse(url).netloc})"
    model_path, _, hf_file = get_config(db_path)
    if hf_file:
        return hf_file
    return os.path.basename(model_path) if model_path else "local"


def status() -> dict:
    return {
        "phase": _phase,
        "available": _phase == "ready",
        "backend": "remote" if get_server_url() else "in-process",
        "model": default_model() if _phase == "ready" else None,
        "error": _model_error if _phase == "error" else None,
    }


# ── Download ───────────────────────────────────────────────────────────────────


def _download(model_path: str, hf_repo: str, hf_file: str) -> str:
    """Download the model from HuggingFace if the file doesn't already exist.

    Returns the path to the local file (may differ from model_path if the
    HF filename differs from the basename of model_path).
    """
    global _phase

    if model_path and os.path.exists(model_path):
        log.debug("LLM model file already exists: %s", model_path)
        return model_path

    if not (hf_repo and hf_file):
        if model_path:
            raise FileNotFoundError(
                f"Model file not found: {model_path}. "
                "Set llm_hf_repo and llm_hf_file to download it automatically."
            )
        raise ValueError("No model path or HuggingFace source configured.")

    _phase = "downloading"
    local_dir = os.path.dirname(model_path) if model_path else "."
    if local_dir:
        os.makedirs(local_dir, exist_ok=True)

    size_hint = ""
    try:
        from huggingface_hub import get_paths_info  # type: ignore[import-untyped]
        infos = list(get_paths_info(hf_repo, [hf_file], repo_type="model"))
        size = getattr(infos[0], "size", None) if infos else None
        if size:
            mb = size / 1_048_576
            size_hint = f" ({mb:.0f} MB)"
    except Exception:
        log.exception("ollama_client: HuggingFace size check")

    log.info("Downloading %s/%s%s from HuggingFace → %s", hf_repo, hf_file, size_hint, local_dir or ".")

    from huggingface_hub import hf_hub_download  # type: ignore[import-untyped]

    t0 = time.monotonic()
    downloaded = hf_hub_download(
        repo_id=hf_repo,
        filename=hf_file,
        local_dir=local_dir or ".",
        token=None,
    )
    elapsed = time.monotonic() - t0
    log.info("Download complete in %.1fs: %s", elapsed, downloaded)

    # If the caller specified a model_path with a different name, rename.
    if model_path and os.path.abspath(downloaded) != os.path.abspath(model_path):
        import shutil
        shutil.move(downloaded, model_path)
        return model_path

    return downloaded


# ── Startup ────────────────────────────────────────────────────────────────────


def start_loading(db_path: Path | None = None) -> None:
    """Begin downloading (if needed) and loading the model in a background thread.

    Safe to call from an async context (e.g. a cog's ``cog_load``).
    Calling again while a load is already in progress is a no-op.
    Pass ``db_path`` so config is read from the database.
    """
    global _ready_event, _phase, _model, _model_error

    if _phase in ("downloading", "loading"):
        return  # already in progress

    if not is_available(db_path):
        return

    server_url = get_server_url()
    if server_url:
        # Nothing to download or load — the server owns the model. Mark ready
        # immediately so callers don't block on an event that never fires.
        event = asyncio.Event()
        event.set()
        _ready_event = event
        _phase = "ready"
        _model = None
        _model_error = None
        log.info("LLM backend: remote llama-server at %s (no local model load).", server_url)
        return

    loop = asyncio.get_event_loop()
    event = asyncio.Event()
    _ready_event = event
    _phase = "idle"
    _model = None
    _model_error = None

    def _work() -> None:
        global _model, _model_error, _phase

        try:
            model_path, hf_repo, hf_file = get_config(db_path)
            effective_path = _download(model_path, hf_repo, hf_file)

            _phase = "loading"
            n_ctx        = int(os.getenv("LLAMA_N_CTX", "4096"))
            n_gpu_layers = int(os.getenv("LLAMA_N_GPU_LAYERS", "0"))
            raw_threads  = os.getenv("LLAMA_N_THREADS", "")
            n_threads    = int(raw_threads) if raw_threads else None
            raw_batch    = os.getenv("LLAMA_N_BATCH", "")
            n_batch      = int(raw_batch) if raw_batch else None

            log.info(
                "Loading LLM model: %s (n_ctx=%d, gpu_layers=%d, threads=%s, batch=%s)",
                effective_path, n_ctx, n_gpu_layers,
                n_threads or "auto", n_batch or "default",
            )

            from llama_cpp import Llama  # type: ignore[import-untyped]

            kwargs: dict = dict(
                model_path=effective_path,
                n_ctx=n_ctx,
                n_gpu_layers=n_gpu_layers,
                verbose=False,
            )
            if n_threads is not None:
                kwargs["n_threads"] = n_threads
            if n_batch is not None:
                kwargs["n_batch"] = n_batch

            t_load = time.monotonic()
            _model = Llama(**kwargs)
            _phase = "ready"
            log.info("LLM model ready in %.1fs.", time.monotonic() - t_load)

        except Exception as exc:
            _model_error = str(exc)
            _phase = "error"
            log.error("LLM setup failed: %s", exc)
        finally:
            loop.call_soon_threadsafe(event.set)

    _executor.submit(_work)


def reload(db_path: Path | None = None) -> None:
    """Trigger a fresh download-check and reload.

    Resets state so the next ``chat()`` call will wait for the new load.
    Safe to call from any context.
    """
    global _ready_event, _phase, _model, _model_error

    if _phase in ("downloading", "loading"):
        log.warning("Reload requested while already loading — ignored.")
        return

    _phase = "idle"
    _model = None
    _model_error = None
    _ready_event = None

    start_loading(db_path)


# ── Inference ──────────────────────────────────────────────────────────────────


async def chat(
    *,
    model: str = "",  # ignored — only one model is loaded at a time
    system: str,
    user_content: str,
    max_tokens: int = 2048,
    temperature: float = 0.7,
) -> str:
    """Run a single-turn chat and return the response text.

    Waits for the model to finish loading if it hasn't yet.

    The ``model`` argument is accepted but **ignored** on both backends: only
    one model is served at a time. It is deliberately not forwarded to the
    remote server — some guild rows carry hosted model IDs (e.g. a Claude
    model name) left over from an abandoned cloud switch, and honouring those
    would silently route moderation content off-box.
    """
    if _ready_event is None:
        raise RuntimeError("LLM not initialised — start_loading() was never called.")

    await _ready_event.wait()

    server_url = get_server_url()
    if server_url:
        log.debug("LLM (remote) request max_tokens=%d system=%.120s user=%.120s",
                  max_tokens, system, user_content)
        return await _remote_chat(
            server_url,
            system=system,
            user_content=user_content,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    if _phase == "error":
        raise RuntimeError(f"LLM failed to load: {_model_error}")

    if _model is None:
        raise RuntimeError("LLM model is not loaded.")

    log.debug("LLM request max_tokens=%d system=%.120s user=%.120s", max_tokens, system, user_content)

    loop = asyncio.get_event_loop()

    def _infer() -> str:
        t0 = time.monotonic()
        result = _model.create_chat_completion(  # type: ignore[union-attr]
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        elapsed = time.monotonic() - t0
        text = (result["choices"][0]["message"]["content"] or "").strip()  # type: ignore[index]
        tokens_out = result.get("usage", {}).get("completion_tokens", "?")  # type: ignore[attr-defined]
        log.debug("LLM response %.1fs %s tokens: %.120s", elapsed, tokens_out, text)
        return text

    return await loop.run_in_executor(_executor, _infer)
