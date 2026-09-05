"""保真度尺子 v3（候选，未接门禁）：分母改从线稿几何取走向，量分前整图平移配准。

**只回答一个问题**：几何锁得死不死——不评美观、不评风格贴合度。与 `fidelity_metric`（现版，下称 v2）
并存：v2 一个字不动，本文件是 v2 的两处改法。改哪两处、为什么改，来自室内透视视角的自证
（render3d `_iteration/run-2026-09-05-metric-perspective-selfcheck/run.md` §四 4 与 §四 2）：

1. **分母基本不含竖直墙线。** v2 第 4 步要线像素"走向有定义"，走向取自 3x3 模糊后的 Sobel 梯度；
   1 px 宽的正竖/正横直线模糊后是 3 px 平台，线像素本身梯度为 0，不进分母。客厅线稿 23711 个
   线像素只有 52% 进分母（正竖直线段 8413 个只进 9%），主卧 65%，揭顶 88%（等轴测下几乎全是斜线）。
   门禁要问"墙在不在原位"，v2 恰恰不看墙角竖线。
2. **出图相对线稿有 6–22 px 的整图错位。** v2 容差 2 px，室内 8 张里 7 张最优位移不在原点，
   按最优位移重算后正确配对散布减半。这种错位算不算"几何错"要用户拍，尺子只报数、不吞掉。

## 算法（与 v2 的差异只在第 4 步与第 6 步；1、2、3、5 步逐字复用 v2 的函数）

1. 结果 LANCZOS 降采样到线稿分辨率（同 v2）。
2. 结果转灰度 → Sobel → 非极大值抑制（同 v2）。
3. 强边阈值＝NMS 后非零像素的 `EDGE_STRENGTH_PERCENTILE` 百分位（同 v2，常量沿用）。
4. **线稿走向从线稿几何取**：对二值线稿的每个线像素，取半径 `ORIENTATION_WINDOW_RADIUS_PX` 圆窗
   内的线像素坐标，对这些坐标做 PCA（等价于结构张量），主轴＝切向，法向＝切向转 90°——与结果侧
   Sobel 给的梯度方向同一口径。1 px 竖直线、水平线、斜线的像素在窗内都是一根各向异性的线段，
   走向都有定义；只有窗内线像素分布各向同性（λ1 = λ2：孤立点、正十字交叉中心）才无定义。
   "无定义"的判据与 v2 同一风格：真正的零（`_MIN_ANISOTROPY_FOR_ORIENTATION`），不是噪声阈值。
5. 命中判据＝位置在 `TOLERANCE_PX` 内 且 走向在 `ANGLE_TOLERANCE_DEG` 内（同 v2，常量沿用，
   无向角二倍角比较）。
6. **配准**：把线稿在 ±`REGISTRATION_SEARCH_RADIUS_PX` 的整数平移网格上逐一挪过去量分（默认只
   平移，不缩放不旋转），取最高分的那个位移。分母固定为原位的全部有定义线像素，挪出画幅的线
   像素算未命中——这样各位移的分数分母相同、可直接比。等分时取离原点最近的位移。**分数与位移
   一起返回**（`FidelityV3Score`），原位分数也保留：`score_at_origin` 只含改法 1，`score` 含
   改法 1+2。可选地再搜 x / y 各自独立的缩放（`scale_candidates`，绕画幅中心；先缩放、再平移），
   输出永远报 (sx, sy, dx, dy)，不搜缩放时 (sx, sy) 恒为 (1, 1)。

## 常量

三个量分常量（`EDGE_STRENGTH_PERCENTILE` / `TOLERANCE_PX` / `ANGLE_TOLERANCE_DEG`）**从 v2 原样
import**，本文件不定新值——v3 相对 v2 的变化只来自上面两处改法，不来自调参。

两个新常量：

- `ORIENTATION_WINDOW_RADIUS_PX = 5`：取走向的 PCA 圆窗半径。这个数是执行者按下面两张实测表选的，
  不是裁决；全表在 `_iteration/run-2026-09-05-metric-v3/结果表.md` 表五、表七。

  窗口小了走向抖：1 px 直线是台阶状的，窗里只看到一两个台阶时主轴偏向台阶的横竖。合成直线
  （已知角度 0°–170° 每 10°，去两端）的法向角误差 p90 最差值，要压在 `ANGLE_TOLERANCE_DEG = 4°`
  里面才不至于让命中判据把自家的线也判掉：

  | 半径 | 1 px 线 | 2 px 线 |
  |---|---|---|
  | 2 | 20.0° | 20.7° |
  | 3 | 10.0° | 11.5° |
  | 4 | 3.8° | 5.0° |
  | 5 | 2.4° | 3.0° |
  | 6 | 2.2° | 3.0° |

  5 是两种线宽都进 4° 的最小半径。真跑样本上的判据随半径同向变化（室内 8 张、配准后，
  "正确 min − 错配 max"除以"正确极差"）：r=2 0.40、r=3 0.78、r=4 0.95、r=5 1.08、r=6 1.16；
  5→6 的增量已在小数点后第二位，而窗越大拐角处两条臂混进同一窗的像素越多（客厅线稿距拐角
  ≤ 半径的线像素占比 r=3 0.43 → r=5 0.47 → r=6 0.48）。进分母占比在所有半径都是 1.000。
- `REGISTRATION_SEARCH_RADIUS_PX = 24`：平移搜索范围。来自自证记录量到的错位 6–22 px（§四 2），
  24 覆盖它们并留 2 px；不是扫出来的，是任务给定。搜索是穷举（49×49），不做粗细两级——
  自证记录里客厅-nofurn-seed3 的分数地形有第二个峰（x+20 处 0.1443 > 原位 0.0739），粗扫可能
  锁错峰。代价是一次量分约 5 s（1280×960 线稿、2.4 万线像素、单核）。

## 已知限制

1. **分数仍只能同输出形态横向比**（v2 限制 1 照旧）：几何全对的底渲自量不是 1.0。
2. **配准会把错配也抬高**：2401 个位移里总能找到一个比原位好的。所以判据要看"配准后的正确配对"
   对"配准后的错配"，不能拿配准后的正确配对对原位的错配——`_iteration` 记录两列都报。
3. **默认只搜平移**：出图 1168×880 与线稿 1280×960 不是等比（1280/1168 = 1.0959，960/880 = 1.0909，
   差 0.46%，画幅边缘相当于约 3 px），各向异性缩放是否解释了一部分"整图错位"，用 `scale_candidates`
   验过，数在 `_iteration/run-2026-09-05-metric-v3/缩放配准.md`；旋转小角度没搜。
4. **走向在拐角处有偏**：PCA 窗内两条臂混在一起时主轴落在对角线上，拐角像素（半径 5 窗内）
   的走向不是任何一条臂的走向。数量占比见 `_iteration` 记录；没剔，剔了就是在动分母口径。
5. **搜索范围是硬边界**：真实错位加对照平移超出 ±24 px 时，报出来的位移停在边界上（自证记录里
   客厅-nofurn-seed1 本身错位 -6 px，再挪 20 px 就到 -26，超界）。位移贴在 ±24 上的结果要当
   "至少这么多"读。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from PIL import Image

from imagegen_worker.fidelity_metric import (
    _LINE_BINARY_THRESHOLD,
    ANGLE_TOLERANCE_DEG,
    EDGE_STRENGTH_PERCENTILE,
    TOLERANCE_PX,
    FidelityMetricError,
    _non_max_suppression,
    _open,
    _rotate90_fit,
    _shift_zero_fill,
    _sobel_components,
)

# ---- 新常量：取值理由见模块 docstring ----
ORIENTATION_WINDOW_RADIUS_PX = 5
REGISTRATION_SEARCH_RADIUS_PX = 24

_MIN_ANISOTROPY_FOR_ORIENTATION = 1e-6
"""窗内线像素坐标协方差两特征值之差低于此数＝各向同性、走向无定义（真正的 0，不是噪声阈值）。"""

_F64 = npt.NDArray[np.float64]
_BOOL = npt.NDArray[np.bool_]
_C128 = npt.NDArray[np.complex128]
_I64 = npt.NDArray[np.int64]


@dataclass(frozen=True)
class FidelityV3Score:
    """v3 的输出：分数与位移一起报，位移不吞掉。

    - `score`：最优 (缩放, 位移) 处的分数（含改法 1+2）。
    - `score_at_origin`：不配准（缩放 1、线稿原位）的分数——只含改法 1，与 v2 直接对照分母改法。
    - `offset_px`：线稿要往右 (dx) / 往下 (dy) 挪多少像素才与结果最对齐；(0, 0)＝原位已最优。
    - `scale_xy`：线稿绕画幅中心沿 x / y 各自缩放多少才最对齐；不搜缩放时恒为 (1.0, 1.0)。
      位移是在这个缩放之后量的：先缩放、再平移。
    - `n_line_pixels` / `n_scored_pixels`：线像素总数 / 进分母的线像素数。
    """

    score: float
    score_at_origin: float
    offset_px: tuple[int, int]
    scale_xy: tuple[float, float]
    n_line_pixels: int
    n_scored_pixels: int

    @property
    def scored_fraction(self) -> float:
        """进分母的线像素占比（v2 在室内视角 0.52–0.65，见模块 docstring）。"""
        return self.n_scored_pixels / self.n_line_pixels


@dataclass(frozen=True)
class _LineOrientation:
    """线稿侧的中间量：法向二倍角与"走向有定义"掩码。"""

    mask: _BOOL
    cos2: _F64
    sin2: _F64
    defined: _BOOL


@dataclass(frozen=True)
class _ScoreGrid:
    """位移搜索的全部中间量（调试与自证用；生产只看 `FidelityV3Score`）。"""

    grid: _F64
    """(2R+1, 2R+1) 的分数网格，`grid[dy - cy + R, dx - cx + R]`，(cx, cy)＝搜索窗中心。"""
    center_offset: tuple[int, int]
    best_offset: tuple[int, int]
    scale_xy: tuple[float, float]
    hits_at_best: _BOOL
    """与线稿同尺寸的命中图：在最优位移下命中的线像素（画在线稿原位，没挪没缩）。"""
    orientation: _LineOrientation

    @property
    def best_score(self) -> float:
        r = (self.grid.shape[0] - 1) // 2
        bx, by = self.best_offset
        cx, cy = self.center_offset
        return float(self.grid[by - cy + r, bx - cx + r])


def _disc_offsets(radius: int) -> list[tuple[int, int]]:
    return [
        (dy, dx)
        for dy in range(-radius, radius + 1)
        for dx in range(-radius, radius + 1)
        if dy * dy + dx * dx <= radius * radius
    ]


def _line_orientation_from_geometry(line_mask: _BOOL, radius: int) -> _LineOrientation:
    """每个线像素的走向＝半径 `radius` 圆窗内线像素坐标的 PCA 主轴；返回法向的二倍角。

    窗内坐标相对中心像素取（不是相对窗内质心），再减去质心——等价于结构张量。逐偏移累加五个
    一阶/二阶矩，不用累计和：坐标平方的累计和在 1280 宽的图上会大数相减丢精度。
    """
    m = line_mask.astype(np.float64)
    h, w = m.shape
    padded = np.pad(m, radius, mode="constant", constant_values=0.0)
    n = np.zeros_like(m)
    sx = np.zeros_like(m)
    sy = np.zeros_like(m)
    sxx = np.zeros_like(m)
    syy = np.zeros_like(m)
    sxy = np.zeros_like(m)
    for dy, dx in _disc_offsets(radius):
        win = padded[radius + dy : radius + dy + h, radius + dx : radius + dx + w]
        n += win
        sx += dx * win
        sy += dy * win
        sxx += (dx * dx) * win
        syy += (dy * dy) * win
        sxy += (dx * dy) * win
    with np.errstate(divide="ignore", invalid="ignore"):
        mx, my = sx / n, sy / n
        cxx = sxx / n - mx * mx
        cyy = syy / n - my * my
        cxy = sxy / n - mx * my
    # 切向二倍角：2θ = atan2(2·cxy, cxx − cyy)；特征值差 = 2·sqrt(((cxx−cyy)/2)² + cxy²)
    half_diff = (cxx - cyy) / 2.0
    anisotropy = 2.0 * np.hypot(half_diff, cxy)
    defined: _BOOL = (
        line_mask & np.isfinite(anisotropy) & (anisotropy > _MIN_ANISOTROPY_FOR_ORIENTATION)
    )
    tangent_2theta = np.arctan2(2.0 * cxy, cxx - cyy)
    # 法向 = 切向 + 90°，二倍角相差 180°：cos/sin 各取负
    cos2 = np.where(defined, -np.cos(tangent_2theta), 0.0)
    sin2 = np.where(defined, -np.sin(tangent_2theta), 0.0)
    return _LineOrientation(mask=line_mask, cos2=cos2, sin2=sin2, defined=defined)


def _result_edge_field(result_img: Image.Image, size: tuple[int, int], percentile: float) -> _C128:
    """结果侧：降采样 → Sobel → NMS → 百分位（同 v2），打包成复数场。

    强边处＝法向二倍角的单位复数，非强边处＝0。

    打包成复数是为了位移搜索时一次 gather 同时拿到"是不是强边"与"朝哪"：非强边处 0 与任何
    走向的内积都是 0，过不了 `cos(2·角度容差)`。
    """
    result_small = result_img.resize(size, Image.Resampling.LANCZOS)
    gray: _F64 = np.asarray(result_small.convert("L"), dtype=np.float64)
    rgx, rgy, rmag = _sobel_components(gray)
    thinned = _non_max_suppression(rmag, rgx, rgy)
    ridge_values = thinned[thinned > 0]
    if ridge_values.size == 0:
        raise FidelityMetricError("写实化结果里一条边都提不出来（不该发生），检查输入图")
    thresh = np.percentile(ridge_values, percentile)
    strong: _BOOL = thinned >= thresh
    angle = np.arctan2(rgy, rgx)
    field: _C128 = np.where(strong, np.exp(2j * angle), 0.0 + 0.0j)
    return field


def _search_offsets(
    orientation: _LineOrientation,
    field: _C128,
    *,
    tolerance_px: int,
    angle_tolerance_deg: float,
    search_radius_px: int,
    scale_xy: tuple[float, float] = (1.0, 1.0),
    center_offset_px: tuple[int, int] = (0, 0),
) -> _ScoreGrid:
    """在以 center_offset_px 为中心、±search_radius_px 的整数平移网格上穷举量分。

    只在有定义的线像素处算（稀疏，万级），不在整幅图上算：每个位移是一次 (K, N) 的 gather，
    K＝容差圆盘内偏移数、N＝进分母的线像素数。分母固定为 N；线像素挪出画幅落在零填充区、
    算未命中。

    `scale_xy` ≠ (1, 1) 时先把线像素坐标绕画幅中心按 x / y 各自缩放、取整，再搜平移；走向不变
    （±1% 的各向异性缩放改角度不到 0.3°）。缩放 1.0 时坐标逐位不变，与不缩放同一条路。
    """
    ys_raw, xs_raw = np.nonzero(orientation.defined)
    n_scored = int(ys_raw.size)
    lc: _F64 = orientation.cos2[ys_raw, xs_raw]
    ls: _F64 = orientation.sin2[ys_raw, xs_raw]
    cos_thresh = float(np.cos(np.radians(2 * angle_tolerance_deg)))

    h, w = orientation.defined.shape
    sx, sy = scale_xy
    cx_img, cy_img = (w - 1) / 2.0, (h - 1) / 2.0
    ys: _I64 = np.rint(cy_img + sy * (ys_raw - cy_img)).astype(np.int64)
    xs: _I64 = np.rint(cx_img + sx * (xs_raw - cx_img)).astype(np.int64)
    excursion = int(max(0, -ys.min(), -xs.min(), ys.max() - (h - 1), xs.max() - (w - 1)))

    cx, cy = center_offset_px
    pad = tolerance_px + search_radius_px + max(abs(cx), abs(cy)) + excursion
    padded = np.pad(field, pad, mode="constant", constant_values=0.0 + 0.0j)
    flat: _C128 = padded.ravel()
    padded_w = padded.shape[1]
    base: _I64 = ((ys + pad) * padded_w + (xs + pad)).astype(np.int64)
    disc = _disc_offsets(tolerance_px)
    k_offsets: _I64 = np.array([dy * padded_w + dx for dy, dx in disc], dtype=np.int64)
    index0: _I64 = base[np.newaxis, :] + k_offsets[:, np.newaxis]

    r = search_radius_px
    side = 2 * r + 1
    grid = np.zeros((side, side), dtype=np.float64)
    best_score = -1.0
    best_offset = (cx, cy)
    best_hits: _BOOL = np.zeros(n_scored, dtype=np.bool_)
    # 等分取离原点最近的：按 (距离², dy, dx) 排序后只在严格更高时更新
    order = sorted(
        (dx * dx + dy * dy, dy, dx)
        for dy in range(cy - r, cy + r + 1)
        for dx in range(cx - r, cx + r + 1)
    )
    for _, dy, dx in order:
        z: _C128 = flat[index0 + (dy * padded_w + dx)]
        match: _BOOL = (lc[np.newaxis, :] * z.real + ls[np.newaxis, :] * z.imag) >= cos_thresh
        hits: _BOOL = match.any(axis=0)
        score = float(hits.sum()) / n_scored
        grid[dy - cy + r, dx - cx + r] = score
        if score > best_score:
            best_score, best_offset, best_hits = score, (dx, dy), hits

    hits_map = np.zeros_like(orientation.defined)
    hits_map[ys_raw[best_hits], xs_raw[best_hits]] = True
    return _ScoreGrid(
        grid=grid,
        center_offset=(cx, cy),
        best_offset=best_offset,
        scale_xy=(float(sx), float(sy)),
        hits_at_best=hits_map,
        orientation=orientation,
    )


def _score_arrays(
    line_png: bytes,
    result_png: bytes,
    *,
    tolerance_px: int,
    percentile: float,
    angle_tolerance_deg: float,
    search_radius_px: int,
    window_radius_px: int,
    shift: tuple[int, int],
    rotate90: bool,
    scale_candidates: Sequence[float] = (1.0,),
) -> tuple[_ScoreGrid, _ScoreGrid, int]:
    """`score_fidelity_v3` 的全中间量版本；`_iteration` 自证脚本用它画命中图、取分数网格。

    `scale_candidates` 里每个 (sx, sy) 组合各搜一遍整个平移窗，取最高分；等分取离 (1, 1) 最近的。
    返回 (最优缩放下的网格, 缩放 (1, 1) 的网格, 线像素总数)：原位分数从第二张的中心取——
    所以 1.0 必须在候选里。
    """
    line_img = _open(line_png, "线稿").convert("L")
    line_arr: npt.NDArray[np.uint8] = np.asarray(line_img, dtype=np.uint8)
    if rotate90:
        line_arr = _rotate90_fit(line_arr)
    dx, dy = shift
    if (dx, dy) != (0, 0):
        line_arr = _shift_zero_fill(line_arr, dx, dy)
    line_mask: _BOOL = line_arr > _LINE_BINARY_THRESHOLD
    n_line = int(line_mask.sum())
    if n_line == 0:
        raise FidelityMetricError("线稿一个边像素都没有（变换后），这组输入量不出东西")

    orientation = _line_orientation_from_geometry(line_mask, window_radius_px)
    if not orientation.defined.any():
        raise FidelityMetricError("线稿边像素全部走向无定义（不该发生），检查输入图")

    result_img = _open(result_png, "写实化结果").convert("RGB")
    field = _result_edge_field(result_img, line_img.size, percentile)
    if 1.0 not in scale_candidates:
        raise FidelityMetricError("缩放候选里必须含 1.0：原位分数从不缩放那张网格取")
    pairs = sorted(
        ((sx - 1.0) ** 2 + (sy - 1.0) ** 2, sy, sx)
        for sx in scale_candidates
        for sy in scale_candidates
    )
    unit: _ScoreGrid | None = None
    best: _ScoreGrid | None = None
    for _, sy, sx in pairs:
        grid = _search_offsets(
            orientation,
            field,
            tolerance_px=tolerance_px,
            angle_tolerance_deg=angle_tolerance_deg,
            search_radius_px=search_radius_px,
            scale_xy=(sx, sy),
        )
        if (sx, sy) == (1.0, 1.0):
            unit = grid
        if best is None or grid.best_score > best.best_score:
            best = grid
    assert best is not None and unit is not None
    return best, unit, n_line


def score_fidelity_v3(
    line_png: bytes,
    result_png: bytes,
    *,
    tolerance_px: int = TOLERANCE_PX,
    percentile: float = EDGE_STRENGTH_PERCENTILE,
    angle_tolerance_deg: float = ANGLE_TOLERANCE_DEG,
    search_radius_px: int = REGISTRATION_SEARCH_RADIUS_PX,
    window_radius_px: int = ORIENTATION_WINDOW_RADIUS_PX,
    shift: tuple[int, int] = (0, 0),
    rotate90: bool = False,
    scale_candidates: Sequence[float] = (1.0,),
) -> FidelityV3Score:
    """(线稿底渲字节, 写实化结果字节) → 分数 + 位移（`FidelityV3Score`）。

    线稿＝render3d 的 `line.png` 原样（黑底白线单通道 0/255），同 v2。
    `shift` / `rotate90` 是自证对照用的旋钮（对线稿做，与 v2 同义），生产路径不传；
    `shift=(20, 0)` 之后 `offset_px` 应回 `(-20, 0)`——这是位移搜索"找得回"的单测。
    `scale_candidates` 默认只有 1.0（只搜平移）；给一串候选就对每个 (sx, sy) 组合各搜一遍平移窗，
    耗时按组合数成倍增长——它是验证用的旋钮（见模块 docstring 限制 3），不是生产默认。
    """
    best, unit, n_line = _score_arrays(
        line_png,
        result_png,
        tolerance_px=tolerance_px,
        percentile=percentile,
        angle_tolerance_deg=angle_tolerance_deg,
        search_radius_px=search_radius_px,
        window_radius_px=window_radius_px,
        shift=shift,
        rotate90=rotate90,
        scale_candidates=scale_candidates,
    )
    r = search_radius_px
    return FidelityV3Score(
        score=best.best_score,
        score_at_origin=float(unit.grid[r, r]),
        offset_px=best.best_offset,
        scale_xy=best.scale_xy,
        n_line_pixels=n_line,
        n_scored_pixels=int(best.orientation.defined.sum()),
    )
