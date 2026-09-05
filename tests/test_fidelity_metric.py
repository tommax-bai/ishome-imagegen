"""保真度尺子搬进本仓后与来源逐数相同：真户型基准 line.png 自量 geometry.png 得 0.0607。

样本是 render3d `_iteration/真户型-基准/底渲-cam-bird-dollhouse/` 那两份（逐字节拷贝进 fixtures），
已知值来自《评审/控制图通路调研-2026-09-02.md》§二（"几何全对的底渲自量只有 0.0607"）。
这条不是在测算法对不对——是在测**搬运没有改变尺子**：常量、卷积、NMS、命中判据任何一处
动了，这个数就对不上。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from imagegen_worker import fidelity_metric

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "真户型-基准-cam-bird-dollhouse"
_LINE_PNG = (_FIXTURES / "line.png").read_bytes()
_GEOMETRY_PNG = (_FIXTURES / "geometry.png").read_bytes()

KNOWN_SELF_SCORE = 0.0607
"""来源脚本对同一对输入的输出（`0.060733`），四位小数记在评审记录里。"""


def test_constants_are_the_ones_from_the_source_script() -> None:
    """三个常量是自证扫出来的，不是默认值——改一个就要重跑自证。"""
    assert fidelity_metric.EDGE_STRENGTH_PERCENTILE == 90.0
    assert fidelity_metric.TOLERANCE_PX == 2
    assert fidelity_metric.ANGLE_TOLERANCE_DEG == 4.0


def test_line_scored_against_its_own_geometry_render_matches_the_known_value() -> None:
    score = fidelity_metric.score_fidelity(_LINE_PNG, _GEOMETRY_PNG)

    assert score == pytest.approx(KNOWN_SELF_SCORE, abs=1e-4)


def test_rotated_control_scores_lower_than_the_correct_pairing() -> None:
    """自证对照用旋转 90°（不是平移——这张图对平移不敏感，见模块 docstring）：
    位置+方向双重判据下，转过的线稿应声下降。"""
    straight = fidelity_metric.score_fidelity(_LINE_PNG, _GEOMETRY_PNG)
    rotated = fidelity_metric.score_fidelity(_LINE_PNG, _GEOMETRY_PNG, rotate90=True)

    assert rotated < straight


def test_same_input_scores_the_same() -> None:
    assert fidelity_metric.score_fidelity(
        _LINE_PNG, _GEOMETRY_PNG
    ) == fidelity_metric.score_fidelity(_LINE_PNG, _GEOMETRY_PNG)


def _png(array: Any, mode: str) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(array, mode=mode).save(buffer, format="PNG")
    return buffer.getvalue()


def test_a_blank_line_drawing_cannot_be_scored() -> None:
    """一个边像素都没有的线稿量不出东西——响亮失败，不给 0 分。"""
    blank = _png(np.zeros((64, 64), dtype=np.uint8), "L")
    result = _png(np.full((64, 64, 3), 128, dtype=np.uint8), "RGB")

    with pytest.raises(fidelity_metric.FidelityMetricError, match="一个边像素都没有"):
        fidelity_metric.score_fidelity(blank, result)


def test_bytes_that_are_not_an_image_fail_loud() -> None:
    with pytest.raises(fidelity_metric.FidelityMetricError, match="解不成图"):
        fidelity_metric.score_fidelity(b"not a png", _GEOMETRY_PNG)
    with pytest.raises(fidelity_metric.FidelityMetricError, match="解不成图"):
        fidelity_metric.score_fidelity(_LINE_PNG, b"not a png")
