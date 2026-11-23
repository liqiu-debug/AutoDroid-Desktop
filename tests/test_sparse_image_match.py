"""Regression for full-frame JPEG recording templates against lossless execution."""
import cv2
import numpy as np
import pytest

from engine.core.utils.image_match import match_image


@pytest.mark.parametrize("side", [60, 100, 160])
@pytest.mark.parametrize("thickness", [1, 2, 3])
def test_sparse_jpeg_template_matches_lossless_frame(side, thickness):
    screen = np.full((side + 80, side + 100, 3), 255, np.uint8)
    center = np.array([30 + side // 2, 40 + side // 2])
    cv2.polylines(screen, [center + np.array([[6, -12], [-6, 0], [6, 12]])], False,
                  (20, 20, 20), thickness, cv2.LINE_AA)
    _, encoded = cv2.imencode(".jpg", screen, [cv2.IMWRITE_JPEG_QUALITY, 90])
    recorded = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    template = recorded[40:40 + side, 30:30 + side]
    result = match_image(screen, template)
    assert result["status"] == "matched", result
    assert result["rect"] == [30, 40, side, side]
    assert result["candidates"][0]["metrics"]["edge_coverage"] >= .88


def test_sparse_jpeg_template_still_rejects_blank_wrong_shape_and_duplicates():
    template = np.full((100, 100, 3), 255, np.uint8)
    cv2.polylines(template, [np.array([[56, 38], [44, 50], [56, 62]])], False, (20, 20, 20), 1, cv2.LINE_AA)
    _, encoded = cv2.imencode(".jpg", template, [cv2.IMWRITE_JPEG_QUALITY, 90])
    compressed = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    blank = np.full((240, 320, 3), 255, np.uint8)
    assert match_image(blank, compressed)["status"] == "not_found"
    wrong = blank.copy()
    wrong[40:140, 30:130] = np.flip(template, axis=1)
    assert match_image(wrong, compressed)["status"] == "not_found"
    duplicate = blank.copy()
    duplicate[20:120, 20:120] = template
    duplicate[130:230, 190:290] = template
    assert match_image(duplicate, compressed)["status"] == "ambiguous"
