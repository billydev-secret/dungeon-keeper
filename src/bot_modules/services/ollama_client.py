"""Local LLM inference via llama-cpp-python.

The model loads (and downloads if needed) in a background thread at startup
so the bot comes online immediately. Any ``chat()`` call that arrives before
loading finishes simply awaits the ready event, then proceeds normally.

Config is read from the database first, falling back to environment variables.
DB keys (all guild_id=0):
    llm_model_path   Full path where the GGUF file is stored/expected.
    llm_hf_repo      HuggingFace repo ID  (e.g. bartowski/Llama-3.2-3B-Instruct-GGUF)
    llm_hf_file      Filename within the repo (e.g. Llama-3.2-3B-Instruct-Q4_K_M.gguf)

Env var fallbacks (used when DB keys are absent):
    LLAMA_MODEL_PATH, LLAMA_HF_REPO, LLAMA_HF_FILE
    LLAMA_N_CTX, LLAMA_N_GPU_LAYERS, LLAMA_N_THREADS
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

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


# ── Public helpers ─────────────────────────────────────────────────────────────


def is_available(db_path: Path | None = None) -> bool:
    model_path, hf_repo, hf_file = get_config(db_path)
    return bool(model_path or (hf_repo and hf_file))


def default_model(db_path: Path | None = None) -> str:
    model_path, _, hf_file = get_config(db_path)
    if hf_file:
        return hf_file
    return os.path.basename(model_path) if model_path else "local"


def status() -> dict:
    return {
        "phase": _phase,
        "available": _phase == "ready",
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
        pass

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
) -> str:
    """Run a single-turn chat and return the response text.

    Waits for the model to finish loading if it hasn't yet.
    """
    if _ready_event is None:
        raise RuntimeError("LLM not initialised — start_loading() was never called.")

    await _ready_event.wait()

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
            temperature=0.7,
        )
        elapsed = time.monotonic() - t0
        text = (result["choices"][0]["message"]["content"] or "").strip()  # type: ignore[index]
        tokens_out = result.get("usage", {}).get("completion_tokens", "?")  # type: ignore[attr-defined]
        log.debug("LLM response %.1fs %s tokens: %.120s", elapsed, tokens_out, text)
        return text

    return await loop.run_in_executor(_executor, _infer)
