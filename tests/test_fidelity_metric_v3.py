"""保真度尺子 v3：分母从线稿几何取走向 + 量分前整图平移配准。

样本同 v2 的测试：真户型基准 `line.png` 自量 `geometry.png`（fixtures 里那两份）。这里的已知值是
v3 在这对输入上**量出来**的数（`_iteration/run-2026-09-05-metric-v3/结果表.md` 表三），不是预设的
目标——它锁的是"v3 没被改动"，与 v2 的 0.0607 是同一种断言。
"""

from __future__ import annotations

import inspect
import io
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageDraw

from imagegen_worker import fidelity_metric, fidelity_metric_v3

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "真户型-基准-cam-bird-dollhouse"
_LINE_PNG = (_FIXTURES / "line.png").read_bytes()
_GEOMETRY_PNG = (_FIXTURES / "geometry.png").read_bytes()

KNOWN_SELF_SCORE_AT_ORIGIN = 0.0675
"""v3 原位（只含分母改法）自量 geometry.png；v2 同一对输入 0.0607。"""
KNOWN_SELF_SCORE_REGISTERED = 0.0690
"""v3 配准后自量 geometry.png，最优位移 (0, +1)。"""
KNOWN_SELF_OFFSET_PX = (0, 1)
KNOWN_LINE_PIXELS = 23022
"""这份线稿的线像素总数；v3 全部进分母（v2 进 20335，占 0.883）。"""

CONTROL_SHIFT_PX = 20


@pytest.fixture(scope="module")
def straight() -> fidelity_metric_v3.FidelityV3Score:
    return fidelity_metric_v3.score_fidelity_v3(_LINE_PNG, _GEOMETRY_PNG)


def test_scoring_constants_default_to_v2s_values_not_new_ones() -> None:
    """三个量分常量沿用现版：v3 相对 v2 的差别只来自两处改法，不来自调参。"""
    defaults = {
        name: p.default
        for name, p in inspect.signature(fidelity_metric_v3.score_fidelity_v3).parameters.items()
    }

    assert defaults["percentile"] == fidelity_metric.EDGE_STRENGTH_PERCENTILE == 90.0
    assert defaults["tolerance_px"] == fidelity_metric.TOLERANCE_PX == 2
    assert defaults["angle_tolerance_deg"] == fidelity_metric.ANGLE_TOLERANCE_DEG == 4.0
    assert defaults["search_radius_px"] == fidelity_metric_v3.REGISTRATION_SEARCH_RADIUS_PX == 24


def test_line_scored_against_its_own_geometry_render_matches_the_known_values(
    straight: fidelity_metric_v3.FidelityV3Score,
) -> None:
    assert straight.score_at_origin == pytest.approx(KNOWN_SELF_SCORE_AT_ORIGIN, abs=1e-4)
    assert straight.score == pytest.approx(KNOWN_SELF_SCORE_REGISTERED, abs=1e-4)
    assert straight.offset_px == KNOWN_SELF_OFFSET_PX
    assert straight.scale_xy == (1.0, 1.0)  # 默认不搜缩放，输出仍报 (sx, sy)


def test_every_line_pixel_of_the_fixture_enters_the_denominator(
    straight: fidelity_metric_v3.FidelityV3Score,
) -> None:
    """改分母的目的：1 px 正竖/正横线也有定义的走向。这份线稿 v3 一个都不漏。"""
    assert straight.n_line_pixels == KNOWN_LINE_PIXELS
    assert straight.n_scored_pixels == KNOWN_LINE_PIXELS
    assert straight.scored_fraction == 1.0


def test_registered_score_is_never_below_the_origin_score(
    straight: fidelity_metric_v3.FidelityV3Score,
) -> None:
    """原点在搜索网格里，最优位移的分数不会低于原位。"""
    assert straight.score >= straight.score_at_origin


def test_rotated_control_scores_far_below_the_correct_pairing(
    straight: fidelity_metric_v3.FidelityV3Score,
) -> None:
    """转 90° 是已知有效的负对照（v2 docstring）；配准救不回来，最高分不到正确配对的十分之一。"""
    rotated = fidelity_metric_v3.score_fidelity_v3(_LINE_PNG, _GEOMETRY_PNG, rotate90=True)

    assert rotated.score * 10 < straight.score


def test_shifted_line_is_found_again_by_the_offset_search(
    straight: fidelity_metric_v3.FidelityV3Score,
) -> None:
    """线稿挪 20 px 后，位移搜索报出的位移正好把它挪回去；分数回到未挪时的值。

    报出的位移是相对未挪线稿的最优位移再加 -20——线稿本身相对结果的那 1 px 错位不因对照消失。
    对照挪的是 x，未挪时的 1 px 错位在 y，两者不打架；挪 x 后 x 方向能不能找回是这条测的。
    """
    shifted = fidelity_metric_v3.score_fidelity_v3(
        _LINE_PNG, _GEOMETRY_PNG, shift=(CONTROL_SHIFT_PX, 0)
    )

    assert shifted.offset_px == (straight.offset_px[0] - CONTROL_SHIFT_PX, straight.offset_px[1])
    assert shifted.score == pytest.approx(straight.score, abs=1e-3)
    assert shifted.score_at_origin < straight.score_at_origin / 5


def test_same_input_scores_the_same(straight: fidelity_metric_v3.FidelityV3Score) -> None:
    assert fidelity_metric_v3.score_fidelity_v3(_LINE_PNG, _GEOMETRY_PNG) == straight


def _stretched_x(png: bytes, factor: float) -> bytes:
    """把结果图绕画幅中心沿 x 拉伸 factor 倍再裁回原尺寸——制造已知的各向异性缩放。"""
    image = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = image.size
    wide = image.resize((round(w * factor), h), Image.Resampling.LANCZOS)
    left = (wide.size[0] - w) // 2
    buffer = io.BytesIO()
    wide.crop((left, 0, left + w, h)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_an_anisotropic_stretch_of_the_result_is_found_by_the_scale_search() -> None:
    """结果沿 x 拉 0.6%（画幅边缘约 4 px，超过 2 px 容差）：只平移的分数掉下去，
    候选里含 1.006 的缩放搜索把它找回来并报出 sx = 1.006、sy = 1.0。
    小平移窗（±6）只为省时间——这条测的是缩放，不是平移范围。"""
    factor = 1.006
    stretched = _stretched_x(_GEOMETRY_PNG, factor)
    candidates = (1.0 - (factor - 1.0), 1.0, factor)

    plain = fidelity_metric_v3.score_fidelity_v3(_LINE_PNG, stretched, search_radius_px=6)
    scaled = fidelity_metric_v3.score_fidelity_v3(
        _LINE_PNG, stretched, search_radius_px=6, scale_candidates=candidates
    )

    assert plain.scale_xy == (1.0, 1.0)
    assert scaled.scale_xy == (factor, 1.0)
    assert scaled.score > plain.score
    assert scaled.score_at_origin == plain.score_at_origin


def test_scale_candidates_must_include_one() -> None:
    with pytest.raises(fidelity_metric.FidelityMetricError, match="必须含 1.0"):
        fidelity_metric_v3.score_fidelity_v3(
            _LINE_PNG, _GEOMETRY_PNG, search_radius_px=2, scale_candidates=(0.99, 1.01)
        )


def _synthetic_line(angle_deg: float, width: int = 1) -> np.ndarray:
    """数组坐标（y 向下）里角度为 angle_deg 的 1 px 直线，300x300 画布正中，长 240。"""
    image = Image.new("L", (300, 300), 0)
    t = np.radians(angle_deg)
    c, half = 150, 120
    ImageDraw.Draw(image).line(
        [
            (c - half * np.cos(t), c - half * np.sin(t)),
            (c + half * np.cos(t), c + half * np.sin(t)),
        ],
        fill=255,
        width=width,
    )
    return np.asarray(image) > 127


@pytest.mark.parametrize("angle_deg", [0.0, 45.0, 90.0])
def test_one_pixel_lines_have_a_defined_orientation_along_their_normal(angle_deg: float) -> None:
    """v2 的分母漏掉的正是这些：正竖、正横、45° 的 1 px 线——v3 每个像素都有走向，且是法向。"""
    mask = _synthetic_line(angle_deg)
    orientation = fidelity_metric_v3._line_orientation_from_geometry(
        mask, fidelity_metric_v3.ORIENTATION_WINDOW_RADIUS_PX
    )

    assert orientation.defined.sum() == mask.sum()
    normal = (np.degrees(np.arctan2(orientation.sin2, orientation.cos2)) / 2) % 180
    expected = (angle_deg + 90) % 180
    error = np.abs(((normal[orientation.defined] - expected) + 90) % 180 - 90)
    assert np.median(error) < 1.0


def test_an_isolated_pixel_has_no_orientation() -> None:
    """走向"无定义"只剩真正各向同性的情形：孤立点。"""
    mask = np.zeros((32, 32), dtype=np.bool_)
    mask[16, 16] = True
    orientation = fidelity_metric_v3._line_orientation_from_geometry(
        mask, fidelity_metric_v3.ORIENTATION_WINDOW_RADIUS_PX
    )

    assert not orientation.defined.any()


def _png(array: Any, mode: str) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(array, mode=mode).save(buffer, format="PNG")
    return buffer.getvalue()


def test_a_blank_line_drawing_cannot_be_scored() -> None:
    blank = _png(np.zeros((64, 64), dtype=np.uint8), "L")
    result = _png(np.full((64, 64, 3), 128, dtype=np.uint8), "RGB")

    with pytest.raises(fidelity_metric.FidelityMetricError, match="一个边像素都没有"):
        fidelity_metric_v3.score_fidelity_v3(blank, result)


def test_bytes_that_are_not_an_image_fail_loud() -> None:
    with pytest.raises(fidelity_metric.FidelityMetricError, match="解不成图"):
        fidelity_metric_v3.score_fidelity_v3(b"not a png", _GEOMETRY_PNG)
    with pytest.raises(fidelity_metric.FidelityMetricError, match="解不成图"):
        fidelity_metric_v3.score_fidelity_v3(_LINE_PNG, b"not a png")
