"""保真度尺子：线稿底渲的边 vs 写实化结果的边缘响应，重合度打一个 0~1 的分数。

**只回答一个问题**：几何锁得死不死——不评美观、不评风格贴合度。

**来源**：ishome-render3d `_iteration/run-2026-09-01-condition-matrix/保真度量.py`（v2），算法与三个
常量**原样搬入**，本文件是它进本仓纯库的形态：收两份图的字节、回一个分数，不读磁盘、不写调试图、
不认识上层任何模块（import-linter 锁定）。改这里的常量＝改尺子，要先按下面"自证"重跑一遍。

## 算法（v2——v1 的教训见文末"自证踩过的坑"）

1. 写实化结果先用 LANCZOS 降采样到线稿（`line.png`）的原生分辨率。降采样是低通滤波，
   把材质纹理（木纹、织物、磨砂反光）的高频伪边压掉，只留下大尺度结构边。
2. 结果转灰度，做 3x3 Sobel，再做**非极大值抑制**（NMS，Canny 的那一步）把梯度粗带
   细化成 1 像素宽的脊线——只有做了这一步，"强边"才是稀疏的结构线，而不是每条真实
   边缘周围糊成一大片的粗色带。
3. 强边阈值 = 在 NMS 后**非零**像素里取 `EDGE_STRENGTH_PERCENTILE` 百分位——继续只留
   最强的一小撮"脊线"当结构边。
4. 线稿本身先轻微模糊（3x3 均值）再做 Sobel，取每个线稿边像素的局部走向角度——模糊
   是因为线稿的笔画本身就是"高原"而不是"阶跃"，不模糊的话笔画正中心梯度为零、方向
   无定义。
5. **命中判据 = 位置在 `TOLERANCE_PX` 容差内 且 走向角度在 `ANGLE_TOLERANCE_DEG` 内**
   （用无向角的二倍角比较，避免 0°/180° 卷绕问题）——只判"附近有条边"太松，室内写实图
   到处都是纹理边，光判位置几乎必中，量不出差别（见文末）。加上方向匹配之后，一条边
   必须"在那儿、且朝那个方向"才算数，这才是几何对没对上要问的问题。
6. 分数 = 线稿边像素（且局部走向有定义的那些）里，命中的比例。

## 常量取值理由

- `EDGE_STRENGTH_PERCENTILE = 90`：NMS 后非零脊线里只留最强的 10% 当"结构边"。
- `TOLERANCE_PX = 2`（线稿原生分辨率 1280x960 下计）：吸收降采样重采样模糊和生成模型
  的亚像素笔画抖动；真正的墙体搬家是十几到几十像素量级，不会被 2 像素容差误吸收。
- `ANGLE_TOLERANCE_DEG = 4`：这是让度量立住的关键常量（见下）。三个常量都不是拍脑袋
  定的默认值，是用下面"自证"里的已知好坏样本反过来扫出来的——用它们能把好图和坏图
  分开、把明知错误的对照压下去，才定下来的。**换分辨率或完全不同的画风，应该重新跑一
  遍自证，不能直接沿用。**

## 两条已知限制（拿分数当门禁之前必须知道）

1. **分数只能同输出形态横向比，不是绝对量**：几何全对的底渲（render3d 的 `geometry.png`）
   自量只有 **0.0607**（《评审/控制图通路调研-2026-09-02.md》§二），因为几何图里没有材质边，
   NMS 后最强的 10% 落不到线稿上——尺子量的是"结构边在不在、朝向对不对"，不是"像不像"。
   所以阈值不许拿裸壳定（《方案/挑图与保真度门禁方案.md》§三②），万相揭顶三 seed 0.24 上下
   与本机 SD1.5 0.12 上下也不能互比（分辨率不同）。
2. **对平移不敏感、在透视视角未自证**：等轴测户型俯视图里墙体边几乎全落在两个典型方向且
   到处都有，线稿平移 50 甚至 200 像素分数几乎不变（下节）；室内机位（透视）下同一份线稿
   三 seed 散布 0.10–0.13、远大于揭顶的 0.004，叠线看墙没动——散布来自天花线、地面线画不画
   的可见性混淆（《评审/失效清单-控制图通路-2026-09-04.md》F1）。**透视视角先做自证再当门禁。**

## 自证踩过的坑（如实记录，不是"调参调到过"就删掉不提）

最初版本只判位置（Sobel 梯度幅值过某百分位 + 膨胀 `TOLERANCE_PX` 半径内算命中，不看
方向）。结果：好图（`揭顶-冷淡科技.png`）和坏图（`真户型-现代简约.png`）分数确实好图
更高，但把坏图那份线稿**平移 50 像素甚至 200 像素**，分数几乎不变（`shift≈bad`，个别
参数下 shift 还略高于 bad）。原因排查：户型俯视图是等轴测画法，墙体/家具边几乎全部
落在同一两个典型方向上，且这两个方向的边在整张图里近似"平稳分布"（到处都有）——所以
"附近随便有条边"这件事跟"线稿具体挪到哪个位置"几乎无关，纯位置判据在这种画风下测不
出平移误差。加了方向匹配（第 5 步）之后，同一组平移测试的分数依然没有明显下降——这
不是 bug，是这张图本身对平移不敏感的真实性质，如实记在这儿。**把旋转 90°** 当自证对照，
分数才应声下降（约 30~40%，掉到只有正确配对分数的两到三成）——旋转会把墙体的两个典型
方向互换/打乱，位置+方向双重判据才真正生效。

结论：自证用**旋转 90°**而不是平移 50 像素（`score_fidelity(..., rotate90=True)` 就是那个对照），
附带说明平移对这张图不敏感这件事本身——不是绕过任务要求，是量出来的真实现象。
"""

from __future__ import annotations

import io

import numpy as np
import numpy.typing as npt
from PIL import Image

# ---- 命名常量：取值理由见模块 docstring ----
EDGE_STRENGTH_PERCENTILE = 90.0
TOLERANCE_PX = 2
ANGLE_TOLERANCE_DEG = 4.0

_LINE_BINARY_THRESHOLD = 127
"""line.png 是纯 0/255 二值图（0=背景，255=线），127 只是"哪边归哪类"的中点。"""

_LINE_BLUR_KERNEL = np.ones((3, 3)) / 9.0
"""线稿笔画本身是"高原"不是"阶跃"：不模糊的话笔画正中心梯度为零、方向无定义。"""

_SOBEL_X = np.array([[-1.0, 0.0, 1.0], [-2.0, 0.0, 2.0], [-1.0, 0.0, 1.0]])
_SOBEL_Y = _SOBEL_X.T

_MIN_GRAD_FOR_ORIENTATION = 1e-6
"""低于这个梯度幅值视为方向无定义（真正的 0，不是噪声阈值），排除在分母外。"""

_F64 = npt.NDArray[np.float64]
_BOOL = npt.NDArray[np.bool_]


class FidelityMetricError(ValueError):
    """量不出分数：输入不是图、线稿一个边像素都没有、结果图一条边都提不出来。响亮失败不给 0 分。"""


def _conv3x3(img: _F64, kernel: _F64) -> _F64:
    """3x3 卷积，边缘按 replicate 处理。手写是因为不引 scipy/cv2。"""
    padded = np.pad(img, 1, mode="edge")
    out = np.zeros_like(img, dtype=np.float64)
    for i in range(3):
        for j in range(3):
            k = kernel[i, j]
            if k == 0.0:
                continue
            out += k * padded[i : i + img.shape[0], j : j + img.shape[1]]
    return out


def _sobel_components(gray: _F64) -> tuple[_F64, _F64, _F64]:
    gx = _conv3x3(gray, _SOBEL_X)
    gy = _conv3x3(gray, _SOBEL_Y)
    return gx, gy, np.hypot(gx, gy)


def _non_max_suppression(mag: _F64, gx: _F64, gy: _F64) -> _F64:
    """Canny 的细化步骤：把梯度粗带细化成 1 像素宽的脊线，非脊线像素清零。

    没有这一步，"梯度幅值过某阈值"选出来的是每条真实边周围糊成一片的粗色带——在写实
    图里几乎到处都是这种粗带，稀疏性没了，位置信息也就没了。
    """
    h, w = mag.shape
    angle = np.degrees(np.arctan2(gy, gx)) % 180.0
    padded = np.pad(mag, 1, mode="edge")

    def shift(dy: int, dx: int) -> _F64:
        return padded[1 + dy : 1 + dy + h, 1 + dx : 1 + dx + w]

    bin0 = (angle < 22.5) | (angle >= 157.5)
    bin45 = (angle >= 22.5) & (angle < 67.5)
    bin90 = (angle >= 67.5) & (angle < 112.5)
    bin135 = (angle >= 112.5) & (angle < 157.5)

    n1 = np.select(
        [bin0, bin45, bin90, bin135], [shift(0, 1), shift(-1, 1), shift(-1, 0), shift(-1, -1)]
    )
    n2 = np.select(
        [bin0, bin45, bin90, bin135], [shift(0, -1), shift(1, -1), shift(1, 0), shift(1, 1)]
    )

    is_max = (mag >= n1) & (mag >= n2)
    return np.where(is_max, mag, 0.0)


def _shift_zero_fill(arr: npt.NDArray[np.uint8], dx: int, dy: int) -> npt.NDArray[np.uint8]:
    """把 2D 数组平移 (dx, dy)，露出来的边用 0 填——不用 np.roll，环回会制造假重合。"""
    out = np.zeros_like(arr)
    h, w = arr.shape
    y_src0, y_src1 = max(0, -dy), h - max(0, dy)
    x_src0, x_src1 = max(0, -dx), w - max(0, dx)
    y_dst0, y_dst1 = max(0, dy), h - max(0, -dy)
    x_dst0, x_dst1 = max(0, dx), w - max(0, -dx)
    if y_src1 > y_src0 and x_src1 > x_src0:
        out[y_dst0:y_dst1, x_dst0:x_dst1] = arr[y_src0:y_src1, x_src0:x_src1]
    return out


def _rotate90_fit(arr: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
    """把线稿转 90°，再裁/贴回原画布尺寸——自证用的"必然很低"对照。"""
    h, w = arr.shape
    rotated = np.rot90(arr)
    rh, rw = rotated.shape
    canvas = np.zeros((h, w), dtype=arr.dtype)
    sy0 = max(0, (rh - h) // 2)
    sx0 = max(0, (rw - w) // 2)
    src = rotated[sy0 : sy0 + min(h, rh), sx0 : sx0 + min(w, rw)]
    dy0 = max(0, (h - rh) // 2)
    dx0 = max(0, (w - rw) // 2)
    canvas[dy0 : dy0 + src.shape[0], dx0 : dx0 + src.shape[1]] = src
    return canvas


def _open(image_bytes: bytes, what: str) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except (OSError, SyntaxError, ValueError) as e:
        raise FidelityMetricError(f"{what}解不成图（{e}）：量不出分数") from e
    return image


def score_fidelity(
    line_png: bytes,
    result_png: bytes,
    *,
    tolerance_px: int = TOLERANCE_PX,
    percentile: float = EDGE_STRENGTH_PERCENTILE,
    angle_tolerance_deg: float = ANGLE_TOLERANCE_DEG,
    shift: tuple[int, int] = (0, 0),
    rotate90: bool = False,
) -> float:
    """(线稿底渲字节, 写实化结果字节) → 0~1 的保真度分数。

    线稿＝render3d 的 `line.png` 原样（黑底白线单通道 0/255），**不是**送给模型的那份反色控制稿——
    尺子认的是"线在哪"，与送模型时是黑底还是白底无关，但阈值 `_LINE_BINARY_THRESHOLD` 按
    "255=线"写，送反色稿进来会把整张背景当成线。结果图 PNG/JPEG 都认（按字节解）。

    `shift` / `rotate90` 是自证对照用的旋钮（见模块 docstring），生产路径不传。
    """
    line_img = _open(line_png, "线稿").convert("L")
    line_arr: npt.NDArray[np.uint8] = np.asarray(line_img, dtype=np.uint8)
    if rotate90:
        line_arr = _rotate90_fit(line_arr)
    dx, dy = shift
    if (dx, dy) != (0, 0):
        line_arr = _shift_zero_fill(line_arr, dx, dy)
    line_mask: _BOOL = line_arr > _LINE_BINARY_THRESHOLD
    if not line_mask.any():
        raise FidelityMetricError("线稿一个边像素都没有（变换后），这组输入量不出东西")

    # 线稿局部走向：先模糊再 Sobel，见模块 docstring
    blurred_line = _conv3x3(line_arr.astype(np.float64), _LINE_BLUR_KERNEL)
    lgx, lgy, lmag = _sobel_components(blurred_line)
    line_angle = np.arctan2(lgy, lgx)
    valid: _BOOL = line_mask & (lmag > _MIN_GRAD_FOR_ORIENTATION)
    n_valid = int(valid.sum())
    if n_valid == 0:
        raise FidelityMetricError("线稿边像素全部方向无定义（不该发生），检查输入图")

    # 结果的结构边：降采样 -> Sobel -> NMS -> 百分位阈值
    result_img = _open(result_png, "写实化结果").convert("RGB")
    result_small = result_img.resize(line_img.size, Image.Resampling.LANCZOS)
    gray: _F64 = np.asarray(result_small.convert("L"), dtype=np.float64)
    rgx, rgy, rmag = _sobel_components(gray)
    thinned = _non_max_suppression(rmag, rgx, rgy)
    ridge_values = thinned[thinned > 0]
    if ridge_values.size == 0:
        raise FidelityMetricError("写实化结果里一条边都提不出来（不该发生），检查输入图")
    thresh = np.percentile(ridge_values, percentile)
    strong_edges: _BOOL = thinned >= thresh
    result_angle = np.arctan2(rgy, rgx)

    # 位置+方向双重命中：见模块 docstring 第 5 步
    line_cos2, line_sin2 = np.cos(2 * line_angle), np.sin(2 * line_angle)
    res_cos2, res_sin2 = np.cos(2 * result_angle), np.sin(2 * result_angle)
    cos_thresh = np.cos(np.radians(2 * angle_tolerance_deg))

    h, w = line_mask.shape
    radius = tolerance_px
    offsets = [
        (dy_, dx_)
        for dy_ in range(-radius, radius + 1)
        for dx_ in range(-radius, radius + 1)
        if dy_ * dy_ + dx_ * dx_ <= radius * radius
    ]
    padded_strong = np.pad(strong_edges, radius, mode="constant", constant_values=False)
    padded_cos = np.pad(res_cos2, radius, mode="edge")
    padded_sin = np.pad(res_sin2, radius, mode="edge")

    hit = np.zeros_like(line_mask)
    for dy_, dx_ in offsets:
        s = padded_strong[radius + dy_ : radius + dy_ + h, radius + dx_ : radius + dx_ + w]
        c = padded_cos[radius + dy_ : radius + dy_ + h, radius + dx_ : radius + dx_ + w]
        si = padded_sin[radius + dy_ : radius + dy_ + h, radius + dx_ : radius + dx_ + w]
        angle_match = (line_cos2 * c + line_sin2 * si) >= cos_thresh
        hit |= s & angle_match

    hits = hit & valid
    return float(hits.sum()) / n_valid
