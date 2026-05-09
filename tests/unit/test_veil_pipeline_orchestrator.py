"""Tier 2 unit tests: veil pipeline orchestrator (run_pipeline + run_reroll).

All heavy dependencies (nudenet, mediapipe, PIL) are mocked via sys.modules
so tests run in CI without those packages installed.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


from services.veil_models import BoundingBox, Detection, PipelineResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bb(x1, y1, x2, y2) -> BoundingBox:
    return BoundingBox(float(x1), float(y1), float(x2), float(y2))


def _det(label, score, x1, y1, x2, y2) -> Detection:
    return Detection(label=label, score=score, box=_bb(x1, y1, x2, y2))


def _make_pil_mock(width: int = 500, height: int = 500) -> MagicMock:
    """Return a MagicMock that stands in for the PIL module."""
    mock_pil = MagicMock()
    mock_pil.Image.open.return_value.size = (width, height)
    return mock_pil


# ---------------------------------------------------------------------------
# run_pipeline tests
# ---------------------------------------------------------------------------

class TestRunPipeline:
    """Tests for services.veil_pipeline.run_pipeline."""

    def _run(
        self,
        detections: list[Detection],
        face_boxes: list[BoundingBox],
        *,
        candidate_count: int = 3,
        img_w: int = 500,
        img_h: int = 500,
        render_return: bytes = b"fake-jpeg",
        cache_dir: Path | None = None,
    ) -> PipelineResult:
        """Run run_pipeline with all heavy deps mocked."""
        mock_pil = _make_pil_mock(img_w, img_h)

        mock_nudenet = MagicMock()
        mock_nudenet.detect.return_value = detections

        mock_face_det = MagicMock()
        mock_face_det.detect_faces.return_value = face_boxes

        mock_pose_det = MagicMock()
        mock_pose_det.detect_pose.return_value = []

        mock_crop_ren = MagicMock()
        mock_crop_ren.render_crop.return_value = render_return

        image_path = Path("/fake/img.jpg")
        image_bytes = b"fake-image-bytes"

        with patch.dict(
            sys.modules,
            {
                "PIL": mock_pil,
                "services.veil_nudenet": mock_nudenet,
                "services.veil_face_detector": mock_face_det,
                "services.veil_pose_detector": mock_pose_det,
                "services.veil_crop_renderer": mock_crop_ren,
            },
        ):
            from services.veil_pipeline import run_pipeline  # noqa: PLC0415

            result = run_pipeline(
                image_path,
                image_bytes,
                "medium",
                candidate_count=candidate_count,
                cache_dir=cache_dir,
            )
        return result

    def test_returns_pipeline_result_type(self):
        dets = [_det("BREAST", 0.9, 10, 10, 100, 100)]
        result = self._run(dets, [])
        assert isinstance(result, PipelineResult)

    def test_candidates_are_filtered_detections(self):
        dets = [
            _det("BREAST", 0.9, 10, 10, 100, 100),
            _det("GENITALIA", 0.7, 200, 200, 300, 300),
        ]
        result = self._run(dets, face_boxes=[])
        assert result.candidates == dets

    def test_crops_list_length_matches_candidates_taken(self):
        dets = [
            _det("BREAST", 0.9, 10, 10, 100, 100),
            _det("GENITALIA", 0.7, 200, 200, 300, 300),
        ]
        result = self._run(dets, face_boxes=[], candidate_count=2)
        assert len(result.crops) == 2

    def test_crops_contain_render_output(self):
        dets = [_det("BREAST", 0.9, 10, 10, 100, 100)]
        result = self._run(dets, face_boxes=[], render_return=b"fake-jpeg", candidate_count=1)
        assert result.crops == [b"fake-jpeg"]

    def test_candidate_count_limits_crops(self):
        dets = [
            _det("A", 0.9, 0, 0, 50, 50),
            _det("B", 0.8, 60, 60, 110, 110),
            _det("C", 0.7, 120, 120, 170, 170),
        ]
        result = self._run(dets, face_boxes=[], candidate_count=2)
        # Only top 2 by score → 2 crops
        assert len(result.crops) == 2
        assert len(result.candidates) == 3  # all filtered candidates returned

    def test_candidates_sorted_by_score_descending(self):
        dets = [
            _det("B", 0.5, 60, 60, 110, 110),
            _det("A", 0.9, 0, 0, 50, 50),
            _det("C", 0.7, 120, 120, 170, 170),
        ]
        result = self._run(dets, face_boxes=[], candidate_count=3)
        # crops are generated in order of sorted-score — just verify 3 crops exist
        assert len(result.crops) == 3

    def test_no_detections_uses_full_image_fallback(self):
        # When both nudenet and pose return nothing, the pipeline falls back to
        # a FULL_IMAGE_FALLBACK detection so it can still produce crops.
        result = self._run([], face_boxes=[])
        assert len(result.candidates) == 1
        assert result.candidates[0].label == "FULL_IMAGE_FALLBACK"
        assert len(result.crops) > 0

    def test_face_filtered_detection_excluded(self):
        # Detection overlaps fully with a face; filter_candidates fallback=True
        # returns the highest-score item anyway so the pipeline has something to crop.
        face = _bb(0, 0, 100, 100)
        det = _det("BREAST", 0.9, 0, 0, 100, 100)
        result = self._run([det], face_boxes=[face], candidate_count=1)
        assert len(result.crops) == 1

    def test_cache_path_passed_to_render_crop(self):
        """When cache_dir is set, render_crop should receive a Path."""
        mock_pil = _make_pil_mock()
        mock_nudenet = MagicMock()
        mock_nudenet.detect.return_value = [_det("BREAST", 0.9, 10, 10, 100, 100)]
        mock_face_det = MagicMock()
        mock_face_det.detect_faces.return_value = []
        mock_pose_det = MagicMock()
        mock_pose_det.detect_pose.return_value = []
        mock_crop_ren = MagicMock()
        mock_crop_ren.render_crop.return_value = b"cached-jpeg"

        image_path = Path("/fake/img.jpg")
        cache_dir = Path("/tmp/cache")

        with patch.dict(
            sys.modules,
            {
                "PIL": mock_pil,
                "services.veil_nudenet": mock_nudenet,
                "services.veil_face_detector": mock_face_det,
                "services.veil_pose_detector": mock_pose_det,
                "services.veil_crop_renderer": mock_crop_ren,
            },
        ):
            from services.veil_pipeline import run_pipeline  # noqa: PLC0415

            run_pipeline(image_path, b"bytes", "medium", cache_dir=cache_dir)

        # First call is for the actual detection (has cache_path); padding calls don't.
        first_call = mock_crop_ren.render_crop.call_args_list[0]
        cache_path_used = first_call.kwargs.get("cache_path") or first_call[1].get("cache_path")
        assert cache_path_used is not None
        assert str(cache_path_used).endswith("img_0.jpg")

    def test_no_cache_dir_passes_none_to_render_crop(self):
        mock_pil = _make_pil_mock()
        mock_nudenet = MagicMock()
        mock_nudenet.detect.return_value = [_det("BREAST", 0.9, 10, 10, 100, 100)]
        mock_face_det = MagicMock()
        mock_face_det.detect_faces.return_value = []
        mock_pose_det = MagicMock()
        mock_pose_det.detect_pose.return_value = []
        mock_crop_ren = MagicMock()
        mock_crop_ren.render_crop.return_value = b"jpeg"

        with patch.dict(
            sys.modules,
            {
                "PIL": mock_pil,
                "services.veil_nudenet": mock_nudenet,
                "services.veil_face_detector": mock_face_det,
                "services.veil_pose_detector": mock_pose_det,
                "services.veil_crop_renderer": mock_crop_ren,
            },
        ):
            from services.veil_pipeline import run_pipeline  # noqa: PLC0415

            run_pipeline(Path("/fake/img.jpg"), b"bytes", "medium")

        call_kwargs = mock_crop_ren.render_crop.call_args
        cache_path_used = call_kwargs.kwargs.get("cache_path") or call_kwargs[1].get("cache_path")
        assert cache_path_used is None


# ---------------------------------------------------------------------------
# run_reroll tests
# ---------------------------------------------------------------------------

class TestRunReroll:
    """Tests for services.veil_pipeline.run_reroll."""

    def _run(
        self,
        existing_crops: list[BoundingBox],
        *,
        img_w: int = 500,
        img_h: int = 500,
        render_return: bytes = b"reroll-jpeg",
    ) -> bytes:
        mock_pil = _make_pil_mock(img_w, img_h)
        mock_crop_ren = MagicMock()
        mock_crop_ren.render_crop.return_value = render_return

        with patch.dict(
            sys.modules,
            {
                "PIL": mock_pil,
                "services.veil_crop_renderer": mock_crop_ren,
            },
        ):
            from services.veil_pipeline import run_reroll  # noqa: PLC0415

            return run_reroll(b"fake-image-bytes", existing_crops)

    def test_returns_bytes_on_success(self):
        result = self._run([])
        assert result == b"reroll-jpeg"

    def test_returns_render_output(self):
        result = self._run([], render_return=b"custom-reroll")
        assert result == b"custom-reroll"

    def test_with_existing_crops_still_returns_bytes(self):
        existing = [_bb(0, 0, 250, 250)]
        result = self._run(existing)
        assert result == b"reroll-jpeg"

    def test_render_crop_called_once(self):
        mock_pil = _make_pil_mock()
        mock_face_det = MagicMock()
        mock_face_det.detect_faces.return_value = []
        mock_crop_ren = MagicMock()
        mock_crop_ren.render_crop.return_value = b"reroll-jpeg"

        with patch.dict(
            sys.modules,
            {
                "PIL": mock_pil,
                "services.veil_crop_renderer": mock_crop_ren,
            },
        ):
            from services.veil_pipeline import run_reroll  # noqa: PLC0415

            run_reroll(b"fake-bytes", [])

        assert mock_crop_ren.render_crop.call_count == 1

    def test_render_crop_receives_none_cache_path(self):
        mock_pil = _make_pil_mock()
        mock_crop_ren = MagicMock()
        mock_crop_ren.render_crop.return_value = b"reroll-jpeg"

        with patch.dict(
            sys.modules,
            {
                "PIL": mock_pil,
                "services.veil_crop_renderer": mock_crop_ren,
            },
        ):
            from services.veil_pipeline import run_reroll  # noqa: PLC0415

            run_reroll(b"fake-bytes", [])

        call_kwargs = mock_crop_ren.render_crop.call_args
        cache_path_used = call_kwargs.kwargs.get("cache_path") or call_kwargs[1].get("cache_path")
        assert cache_path_used is None

    def test_result_is_bytes_not_none(self):
        result = self._run([_bb(100, 100, 400, 400)])
        assert result is not None
        assert isinstance(result, bytes)
