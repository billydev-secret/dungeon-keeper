"""Tests for atomic mediapipe model download.

An interrupted download must not leave a partial file at the final path —
otherwise mediapipe will pick up the corrupt cache on the next run and fail
opaquely. The download writes to <model>.tmp first, then renames atomically.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


def _make_dl(target: Path, content: bytes):
    """Returns a function emulating urlretrieve(url, dest_path)."""
    def _fake_dl(_url, dest):
        Path(dest).write_bytes(content)
    return _fake_dl


def _make_failing_dl():
    """Simulates a network drop AFTER bytes have already started landing on
    disk — this is the real-world failure mode where naive code leaves a
    partial file at the final path."""
    def _fail(_url, dest):
        Path(dest).write_bytes(b"partial-corrupt-bytes")
        raise OSError("simulated network drop mid-download")
    return _fail


def test_face_model_download_uses_tmp_then_renames(tmp_path: Path, monkeypatch):
    """Successful download lands at the final path with the right contents."""
    import bot_modules.services.guess_face_detector as fd
    monkeypatch.setattr(fd, "_MODELS_DIR", tmp_path)

    target = tmp_path / "blaze_face_short_range.tflite"
    with patch("bot_modules.services.guess_face_detector.urllib.request.urlretrieve",
               side_effect=_make_dl(target, b"x" * 1024)):
        result = fd._ensure_face_model()

    assert result == target
    assert target.exists()
    assert target.read_bytes() == b"x" * 1024
    # Tmp must not linger after a successful rename.
    assert not (tmp_path / "blaze_face_short_range.tflite.tmp").exists()


def test_face_model_download_failure_leaves_no_partial_file(tmp_path: Path, monkeypatch):
    """An interrupted download must not poison the final path or the tmp slot
    — next run must be free to retry cleanly."""
    import bot_modules.services.guess_face_detector as fd
    monkeypatch.setattr(fd, "_MODELS_DIR", tmp_path)

    target = tmp_path / "blaze_face_short_range.tflite"
    with patch("bot_modules.services.guess_face_detector.urllib.request.urlretrieve",
               side_effect=_make_failing_dl()):
        with pytest.raises(OSError):
            fd._ensure_face_model()

    assert not target.exists()
    assert not (tmp_path / "blaze_face_short_range.tflite.tmp").exists()


def test_pose_model_download_uses_tmp_then_renames(tmp_path: Path, monkeypatch):
    import bot_modules.services.guess_pose_detector as pd
    monkeypatch.setattr(pd, "_MODELS_DIR", tmp_path)

    target = tmp_path / "pose_landmarker_full.task"
    with patch("bot_modules.services.guess_pose_detector.urllib.request.urlretrieve",
               side_effect=_make_dl(target, b"y" * 2048)):
        result = pd._ensure_pose_model()

    assert result == target
    assert target.exists()
    assert target.read_bytes() == b"y" * 2048
    assert not (tmp_path / "pose_landmarker_full.task.tmp").exists()


def test_pose_model_download_failure_leaves_no_partial_file(tmp_path: Path, monkeypatch):
    import bot_modules.services.guess_pose_detector as pd
    monkeypatch.setattr(pd, "_MODELS_DIR", tmp_path)

    target = tmp_path / "pose_landmarker_full.task"
    with patch("bot_modules.services.guess_pose_detector.urllib.request.urlretrieve",
               side_effect=_make_failing_dl()):
        with pytest.raises(OSError):
            pd._ensure_pose_model()

    assert not target.exists()
    assert not (tmp_path / "pose_landmarker_full.task.tmp").exists()
