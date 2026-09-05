"""保真度尺子 v2（现版 `fidelity_metric`）对 v3（`fidelity_metric_v3`）的对照矩阵（2026-09-05）。

只做分析：不改 `src/` 里任何一份已有文件、不调模型。样本全部是 render3d `_iteration/` 里的真跑存档，
对照设计照抄 render3d `_iteration/run-2026-09-05-metric-perspective-selfcheck/度量自证-室内视角.py`。

产出（全在本目录）：
    结果.json          全部数字（含每张图每个位移的 49×49 分数网格、各窗口半径的全套分数）
    结果表.md          自动生成的表格（run.md 引用其中的表）
    montage-叠线命中.png   12 张出图：线稿按最优位移叠上去（红）、命中（绿）
    分母对照.png           三份线稿：黄＝v2 就进分母、青＝v3 新进分母、灰＝仍不进
    运行日志.txt           标准输出（由调用方重定向）

用法（仓根目录）：uv run python _iteration/run-2026-09-05-metric-v3/度量对照-v2对v3.py
"""

# ruff: noqa: E501  —— 表格行按列写成一行，折了反而对不上列；只放开行宽，其余规则照常
from __future__ import annotations

import json
import pathlib
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from imagegen_worker import fidelity_metric as V2
from imagegen_worker import fidelity_metric_v3 as V3

HERE = pathlib.Path(__file__).resolve().parent
R3 = pathlib.Path("/Users/baitianxing/codes/ishome-render3d/_iteration")
CAT = R3 / "run-2026-09-04-failure-catalogue"
ROOM_IMG_DIR = CAT / "万相-室内机位"
BIRD_IMG_DIR = R3 / "run-2026-09-02-control-path-survey" / "万相-doodle"
BIRD = R3 / "真户型-基准" / "底渲-cam-bird-dollhouse"

LINES = {
    "客厅": CAT / "底渲-室内机位" / "cam-room-客厅" / "line.png",
    "主卧": CAT / "底渲-室内机位" / "cam-room-主卧" / "line.png",
    "揭顶": BIRD / "line.png",
}
GEOMS = {
    "客厅": CAT / "底渲-室内机位" / "cam-room-客厅" / "geometry.png",
    "主卧": CAT / "底渲-室内机位" / "cam-room-主卧" / "geometry.png",
    "揭顶": BIRD / "geometry.png",
}
# 错配＝拿另一份线稿量本图：室内互换（同相机模型换房间）；揭顶没有第二张揭顶线稿，用客厅线稿（跨视角）
MISMATCH = {"客厅": "主卧", "主卧": "客厅", "揭顶": "客厅"}

ROOM_IMAGES = {
    "客厅": ["客厅-nofurn-seed1", "客厅-nofurn-seed2", "客厅-nofurn-seed3", "客厅-default-seed1"],
    "主卧": ["主卧-nofurn-seed1", "主卧-nofurn-seed2", "主卧-nofurn-seed3", "主卧-default-seed1"],
}
BIRD_IMAGES = ["run1-seed12345", "run2-seed12345-repeat", "run4-seed1001", "run5-seed1002"]

SUBJECTS: list[dict[str, Any]] = (
    [
        {"name": n, "view": "室内", "line": r, "kind": "出图", "path": ROOM_IMG_DIR / f"{n}.png"}
        for r in ROOM_IMAGES
        for n in ROOM_IMAGES[r]
    ]
    + [
        {
            "name": f"揭顶 {n}",
            "view": "揭顶",
            "line": "揭顶",
            "kind": "出图",
            "path": BIRD_IMG_DIR / f"{n}.png",
        }
        for n in BIRD_IMAGES
    ]
    + [
        {
            "name": f"{r} geometry.png（自量）",
            "view": "室内" if r != "揭顶" else "揭顶",
            "line": r,
            "kind": "geometry",
            "path": GEOMS[r],
        }
        for r in LINES
    ]
)

CONTROL_SHIFT_PX = 20
RECOVERY_SCORE_TOLERANCE = 0.005
"""平移对照"分数找回"的口径：配准后分数与未挪时相差不超过这个数——挪出画幅的少量线像素会让分母略变。"""
VARIANTS = ["正确", "错配", "转90", "平移x20", "平移y20"]
RADII = [2, 3, 4, 5, 6]
DEFAULT_RADIUS = V3.ORIENTATION_WINDOW_RADIUS_PX
assert DEFAULT_RADIUS in RADII


def variant_line_and_kwargs(line_key: str, variant: str) -> tuple[str, dict[str, Any]]:
    if variant == "正确":
        return line_key, {}
    if variant == "错配":
        return MISMATCH[line_key], {}
    if variant == "转90":
        return line_key, {"rotate90": True}
    if variant == "平移x20":
        return line_key, {"shift": (CONTROL_SHIFT_PX, 0)}
    if variant == "平移y20":
        return line_key, {"shift": (0, CONTROL_SHIFT_PX)}
    raise ValueError(variant)


_BYTES_CACHE: dict[pathlib.Path, bytes] = {}


def _bytes(p: pathlib.Path) -> bytes:
    if p not in _BYTES_CACHE:
        _BYTES_CACHE[p] = p.read_bytes()
    return _BYTES_CACHE[p]


def job(subject: dict[str, Any], variant: str, radius: int | None) -> dict[str, Any]:
    """radius=None → v2；否则 v3 在该窗口半径下。返回可 json 的数字 + 正确配对的命中图（packbits）。"""
    line_key, kw = variant_line_and_kwargs(subject["line"], variant)
    line_png, result_png = _bytes(LINES[line_key]), _bytes(subject["path"])
    t0 = time.time()
    out: dict[str, Any] = {"subject": subject["name"], "variant": variant, "radius": radius}
    if radius is None:
        out["v2"] = V2.score_fidelity(line_png, result_png, **kw)
    else:
        grid, _unit, n_line = V3._score_arrays(
            line_png,
            result_png,
            tolerance_px=V2.TOLERANCE_PX,
            percentile=V2.EDGE_STRENGTH_PERCENTILE,
            angle_tolerance_deg=V2.ANGLE_TOLERANCE_DEG,
            search_radius_px=V3.REGISTRATION_SEARCH_RADIUS_PX,
            window_radius_px=radius,
            shift=kw.get("shift", (0, 0)),
            rotate90=kw.get("rotate90", False),
        )
        r = V3.REGISTRATION_SEARCH_RADIUS_PX
        bx, by = grid.best_offset
        out.update(
            {
                "v3_origin": float(grid.grid[r, r]),
                "v3_best": float(grid.grid[by + r, bx + r]),
                "offset": [bx, by],
                "n_line": n_line,
                "n_scored": int(grid.orientation.defined.sum()),
                "grid": grid.grid.round(5).tolist(),
            }
        )
        if variant == "正确":
            out["hits_packed"] = np.packbits(grid.hits_at_best).tobytes()
            out["hits_shape"] = list(grid.hits_at_best.shape)
    out["seconds"] = round(time.time() - t0, 1)
    return out


# ---------------------------------------------------------------- 分母：线像素按局部形状分类（照抄 边像素有效性.py）
def shape_classes(line_mask: np.ndarray) -> dict[str, np.ndarray]:
    up = np.zeros_like(line_mask)
    up[1:] = line_mask[:-1]
    down = np.zeros_like(line_mask)
    down[:-1] = line_mask[1:]
    left = np.zeros_like(line_mask)
    left[:, 1:] = line_mask[:, :-1]
    right = np.zeros_like(line_mask)
    right[:, :-1] = line_mask[:, 1:]
    vertical = line_mask & up & down & ~left & ~right
    horizontal = line_mask & left & right & ~up & ~down
    other = line_mask & ~vertical & ~horizontal
    return {"正竖直线段": vertical, "正横直线段": horizontal, "其他（斜线/拐点/粗处）": other}


def v2_valid(line_arr: np.ndarray) -> np.ndarray:
    mask = line_arr > V2._LINE_BINARY_THRESHOLD
    blurred = V2._conv3x3(line_arr.astype(np.float64), V2._LINE_BLUR_KERNEL)
    _, _, lmag = V2._sobel_components(blurred)
    return mask & (lmag > V2._MIN_GRAD_FOR_ORIENTATION)


def junction_neighbourhood(mask: np.ndarray, radius: int) -> np.ndarray:
    """距"交叉/拐角像素"（8 邻域内线像素 ≥ 3 个）≤ radius 的线像素——PCA 窗里会混进另一条臂的那些。"""
    h, w = mask.shape
    p = np.pad(mask, 1, mode="constant", constant_values=False)
    nb = np.zeros(mask.shape, dtype=np.int64)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            nb += p[1 + dy : 1 + dy + h, 1 + dx : 1 + dx + w]
    junction = mask & (nb >= 3)
    pj = np.pad(junction, radius, mode="constant", constant_values=False)
    near = np.zeros_like(mask)
    for dy, dx in V3._disc_offsets(radius):
        near |= pj[radius + dy : radius + dy + h, radius + dx : radius + dx + w]
    return near & mask


# ---------------------------------------------------------------- 合成线：各窗口半径下法向角误差
def synthetic_angle_table() -> tuple[list[str], dict[str, Any]]:
    def line_mask(deg: float, width: int, size: int = 300, length: int = 120) -> np.ndarray:
        im = Image.new("L", (size, size), 0)
        d = ImageDraw.Draw(im)
        c = size // 2
        t = np.radians(deg)
        d.line(
            [
                (c - length * np.cos(t), c - length * np.sin(t)),
                (c + length * np.cos(t), c + length * np.sin(t)),
            ],
            fill=255,
            width=width,
        )
        return np.asarray(im) > 127

    degs = list(range(0, 180, 10))
    md = [
        "| 窗口半径 | 线宽 px | " + " | ".join(f"{a}°" for a in degs) + " | 最差 p90 |",
        "|---|---|" + "---|" * len(degs) + "---|",
    ]
    data: dict[str, Any] = {}
    for width in (1, 2):
        for r in RADII:
            cells, worst = [], 0.0
            for deg in degs:
                m = line_mask(deg, width)
                o = V3._line_orientation_from_geometry(m, r)
                ys, xs = np.nonzero(o.defined)
                t = np.radians(deg)
                proj = xs * np.cos(t) + ys * np.sin(t)
                keep = (proj > proj.min() + r + 1) & (proj < proj.max() - r - 1)  # 去两端
                ang = (
                    np.degrees(np.arctan2(o.sin2[ys[keep], xs[keep]], o.cos2[ys[keep], xs[keep]]))
                    / 2
                ) % 180
                diff = np.abs(((ang - (deg + 90)) + 90) % 180 - 90)
                med, p90 = float(np.median(diff)), float(np.percentile(diff, 90))
                cells.append(f"{med:.1f} / {p90:.1f}")
                worst = max(worst, p90)
                data[f"r{r}-w{width}-{deg}"] = {"median": med, "p90": p90}
            md.append(f"| {r} | {width} | " + " | ".join(cells) + f" | {worst:.1f} |")
    return md, data


# ---------------------------------------------------------------- 主流程
def fmt(x: float | None) -> str:
    return "—" if x is None else f"{x:.4f}"


def fmt_off(o: list[int] | None) -> str:
    return "—" if o is None else f"({o[0]:+d},{o[1]:+d})"


def font(px: int) -> ImageFont.ImageFont:
    for p in (
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ):
        try:
            return ImageFont.truetype(p, px)
        except OSError:
            continue
    return ImageFont.load_default()


def main() -> int:
    t0 = time.time()
    print(
        f"v2 常量 percentile {V2.EDGE_STRENGTH_PERCENTILE} / tol {V2.TOLERANCE_PX} px / angle {V2.ANGLE_TOLERANCE_DEG}°",
        flush=True,
    )
    print(
        f"v3 常量 窗口半径 {DEFAULT_RADIUS}（扫 {RADII}）/ 搜索半径 ±{V3.REGISTRATION_SEARCH_RADIUS_PX} px",
        flush=True,
    )

    jobs = [(s, v, None) for s in SUBJECTS for v in VARIANTS] + [
        (s, v, r) for r in RADII for s in SUBJECTS for v in VARIANTS
    ]
    print(
        f"任务 {len(jobs)} 个（v2 {len(SUBJECTS) * len(VARIANTS)}，v3 {len(RADII)} 个半径 × {len(SUBJECTS) * len(VARIANTS)}）",
        flush=True,
    )
    results: dict[tuple[str, str, int | None], dict[str, Any]] = {}
    with ProcessPoolExecutor() as pool:
        for k, res in enumerate(pool.map(job, *zip(*jobs, strict=True), chunksize=2)):
            results[(res["subject"], res["variant"], res["radius"])] = res
            if k % 25 == 0:
                print(f"  {k + 1}/{len(jobs)} {time.time() - t0:.0f}s", flush=True)
    print(f"量分完成 {time.time() - t0:.0f}s", flush=True)

    def v2s(name: str, variant: str) -> float:
        return float(results[(name, variant, None)]["v2"])

    def v3r(name: str, variant: str, radius: int = DEFAULT_RADIUS) -> dict[str, Any]:
        return results[(name, variant, radius)]

    md: list[str] = []
    out: dict[str, Any] = {
        "常量": {
            "percentile": V2.EDGE_STRENGTH_PERCENTILE,
            "tolerance_px": V2.TOLERANCE_PX,
            "angle_deg": V2.ANGLE_TOLERANCE_DEG,
            "v3_窗口半径": DEFAULT_RADIUS,
            "v3_搜索半径": V3.REGISTRATION_SEARCH_RADIUS_PX,
            "扫描的窗口半径": RADII,
        }
    }

    # ---- 表一/二/三：逐图 v2 vs v3（默认窗口半径）----
    def per_image_table(title: str, subjects: list[dict[str, Any]]) -> None:
        md.append(f"\n## {title}\n")
        md.append(
            "| 出图 | v2 正确 | v2 错配 | v2 转90 | v2 平移x20 | v2 平移y20 | "
            "v3原位 正确 | v3原位 错配 | v3原位 转90 | "
            "v3配准 正确（位移） | v3配准 错配（位移） | v3配准 转90（位移） | v3配准 平移x20（位移） | v3配准 平移y20（位移） | 位移找回 | 分数找回 |"
        )
        md.append("|---|" + "---|" * 15)
        for s in subjects:
            n = s["name"]
            ok, x20, y20 = v3r(n, "正确"), v3r(n, "平移x20"), v3r(n, "平移y20")
            # 位移找回＝报出的位移正好是未挪时的位移再减 20；分数找回＝配准后分数回到未挪时的值（±0.005）
            recovered_x = [
                x20["offset"][0] - ok["offset"][0],
                x20["offset"][1] - ok["offset"][1],
            ] == [-CONTROL_SHIFT_PX, 0]
            recovered_y = [
                y20["offset"][0] - ok["offset"][0],
                y20["offset"][1] - ok["offset"][1],
            ] == [0, -CONTROL_SHIFT_PX]
            score_x = abs(x20["v3_best"] - ok["v3_best"]) <= RECOVERY_SCORE_TOLERANCE
            score_y = abs(y20["v3_best"] - ok["v3_best"]) <= RECOVERY_SCORE_TOLERANCE
            cells = (
                [
                    fmt(v2s(n, "正确")),
                    fmt(v2s(n, "错配")),
                    fmt(v2s(n, "转90")),
                    fmt(v2s(n, "平移x20")),
                    fmt(v2s(n, "平移y20")),
                    fmt(ok["v3_origin"]),
                    fmt(v3r(n, "错配")["v3_origin"]),
                    fmt(v3r(n, "转90")["v3_origin"]),
                ]
                + [f"{fmt(v3r(n, v)['v3_best'])} {fmt_off(v3r(n, v)['offset'])}" for v in VARIANTS]
                + [
                    "x" + ("✓" if recovered_x else "✗") + " y" + ("✓" if recovered_y else "✗"),
                    "x" + ("✓" if score_x else "✗") + " y" + ("✓" if score_y else "✗"),
                ]
            )
            md.append(f"| {n} | " + " | ".join(cells) + " |")

    room_subjects = [s for s in SUBJECTS if s["view"] == "室内" and s["kind"] == "出图"]
    bird_subjects = [s for s in SUBJECTS if s["view"] == "揭顶" and s["kind"] == "出图"]
    geom_subjects = [s for s in SUBJECTS if s["kind"] == "geometry"]
    per_image_table(
        f"表一 室内 8 张出图 · v2 对 v3（窗口半径 {DEFAULT_RADIUS}；错配＝另一间线稿；位移＝线稿要挪的 (dx 右, dy 下) px）",
        room_subjects,
    )
    per_image_table("表二 揭顶 4 张出图（错配＝客厅线稿，跨视角）", bird_subjects)
    per_image_table("表三 底渲 geometry.png 自量（几何全对的基线）", geom_subjects)

    out["逐图"] = {
        f"{k[0]}|{k[1]}|{'v2' if k[2] is None else f'r{k[2]}'}": {
            kk: val for kk, val in res.items() if kk not in ("hits_packed", "hits_shape", "grid")
        }
        for k, res in results.items()
    }
    out["位移网格"] = {
        f"{k[0]}|{k[1]}|r{k[2]}": res["grid"]
        for k, res in results.items()
        if k[2] == DEFAULT_RADIUS
    }

    # ---- 表四：进分母占比 改前改后 ----
    md.append("\n## 表四 进分母的线像素（v2＝模糊后 Sobel 幅值 > 0；v3＝PCA 窗内各向异性 > 0）\n")
    md.append(
        "| 线稿 | 线像素总数 | v2 进分母（占比） | v3 进分母（占比） | 正竖直线段：总 / v2 / v3 | 正横直线段：总 / v2 / v3 | 其他：总 / v2 / v3 |"
    )
    md.append("|---|---|---|---|---|---|---|")
    denom: dict[str, Any] = {}
    line_arrays = {
        k: np.asarray(Image.open(p).convert("L"), dtype=np.uint8) for k, p in LINES.items()
    }
    panels = []
    for key, arr in line_arrays.items():
        mask = arr > V2._LINE_BINARY_THRESHOLD
        valid2 = v2_valid(arr)
        defined3 = V3._line_orientation_from_geometry(mask, DEFAULT_RADIUS).defined
        cls = shape_classes(mask)
        cells = [
            f"{int(m.sum())} / {int((m & valid2).sum())} / {int((m & defined3).sum())}"
            for m in cls.values()
        ]
        denom[key] = {
            "总": int(mask.sum()),
            "v2": int(valid2.sum()),
            "v3": int(defined3.sum()),
            "按形状": {
                c: {
                    "总": int(m.sum()),
                    "v2": int((m & valid2).sum()),
                    "v3": int((m & defined3).sum()),
                }
                for c, m in cls.items()
            },
            "各半径_v3": {
                r: int(V3._line_orientation_from_geometry(mask, r).defined.sum()) for r in RADII
            },
            "各半径_距拐角内": {r: int(junction_neighbourhood(mask, r).sum()) for r in RADII},
        }
        md.append(
            f"| {key} line.png | {int(mask.sum())} | {int(valid2.sum())}（{valid2.sum() / mask.sum():.3f}） | "
            f"{int(defined3.sum())}（{defined3.sum() / mask.sum():.3f}） | "
            + " | ".join(cells)
            + " |"
        )
        canvas = np.zeros((*mask.shape, 3), dtype=np.uint8)
        canvas[mask & ~valid2 & ~defined3] = (110, 110, 110)
        canvas[valid2] = (255, 255, 0)
        canvas[defined3 & ~valid2] = (0, 220, 220)
        panels.append((key, Image.fromarray(canvas)))
    out["分母"] = denom
    md.append(
        "\n各窗口半径下 v3 进分母数 / 距拐角（8 邻域内线像素 ≥ 3 的像素）≤ 半径的线像素数：\n"
    )
    md.append("| 线稿 | " + " | ".join(f"r={r}" for r in RADII) + " |")
    md.append("|---|" + "---|" * len(RADII))
    for key in LINES:
        md.append(
            f"| {key} 进分母 | "
            + " | ".join(
                f"{denom[key]['各半径_v3'][r]}（{denom[key]['各半径_v3'][r] / denom[key]['总']:.3f}）"
                for r in RADII
            )
            + " |"
        )
        md.append(
            f"| {key} 距拐角内 | "
            + " | ".join(
                f"{denom[key]['各半径_距拐角内'][r]}（{denom[key]['各半径_距拐角内'][r] / denom[key]['总']:.3f}）"
                for r in RADII
            )
            + " |"
        )

    # ---- 表五：判据——间隙 / 极差 / 比值 ----
    md.append("\n## 表五 判据：正确配对最小值 − 错配最大值（间隙）对正确配对自身极差\n")
    md.append(
        "| 样本组 | 尺子 | 正确 min | 正确 max | 正确极差 | 错配 max | 转90 max | 间隙 | 间隙/极差 | min正/max错 |"
    )
    md.append("|---|---|---|---|---|---|---|---|---|---|")
    crit: dict[str, Any] = {}

    def criterion(
        group: str,
        subjects: list[dict[str, Any]],
        label: str,
        ok: list[float],
        mis: list[float],
        rot: list[float],
    ) -> None:
        gap = min(ok) - max(mis)
        spread = max(ok) - min(ok)
        row = {
            "正确min": min(ok),
            "正确max": max(ok),
            "正确极差": spread,
            "错配max": max(mis),
            "转90max": max(rot),
            "间隙": gap,
            "间隙/极差": gap / spread if spread > 0 else None,
            "min正/max错": min(ok) / max(mis) if max(mis) > 0 else None,
        }
        crit[f"{group}|{label}"] = row
        md.append(
            f"| {group} | {label} | {min(ok):.4f} | {max(ok):.4f} | {spread:.4f} | {max(mis):.4f} | {max(rot):.4f} | "
            f"{gap:+.4f} | {fmt(row['间隙/极差'])} | {fmt(row['min正/max错'])} |"
        )

    for group, subjects in (("室内 8 张", room_subjects), ("揭顶 4 张", bird_subjects)):
        names = [s["name"] for s in subjects]
        criterion(
            group,
            subjects,
            "v2",
            [v2s(n, "正确") for n in names],
            [v2s(n, "错配") for n in names],
            [v2s(n, "转90") for n in names],
        )
        for r in RADII:
            tag = f"r={r}" + ("（默认）" if r == DEFAULT_RADIUS else "")
            criterion(
                group,
                subjects,
                f"v3 原位 {tag}",
                [v3r(n, "正确", r)["v3_origin"] for n in names],
                [v3r(n, "错配", r)["v3_origin"] for n in names],
                [v3r(n, "转90", r)["v3_origin"] for n in names],
            )
            criterion(
                group,
                subjects,
                f"v3 配准 {tag}",
                [v3r(n, "正确", r)["v3_best"] for n in names],
                [v3r(n, "错配", r)["v3_best"] for n in names],
                [v3r(n, "转90", r)["v3_best"] for n in names],
            )
    out["判据"] = crit

    # ---- 表六：各窗口半径下逐图正确配对分数与位移（看位移随半径稳不稳）----
    md.append("\n## 表六 窗口半径扫描：正确配对的 v3 配准分数（位移），逐图\n")
    md.append("| 出图 | " + " | ".join(f"r={r}" for r in RADII) + " |")
    md.append("|---|" + "---|" * len(RADII))
    for s in room_subjects + bird_subjects + geom_subjects:
        n = s["name"]
        md.append(
            f"| {n} | "
            + " | ".join(
                f"{v3r(n, '正确', r)['v3_best']:.4f} {fmt_off(v3r(n, '正确', r)['offset'])}"
                for r in RADII
            )
            + " |"
        )

    # ---- 表七：合成线法向角误差 ----
    md.append(
        "\n## 表七 合成 1 px / 2 px 直线（已知角度，去两端）：v3 法向角误差 中位 / p90（度），按窗口半径\n"
    )
    syn_md, syn_data = synthetic_angle_table()
    md.extend(syn_md)
    out["合成线角误差"] = syn_data

    # ---- 拼图：12 张出图，线稿按最优位移叠上去（红）、命中（绿）----
    size = (line_arrays["客厅"].shape[1], line_arrays["客厅"].shape[0])
    tile_w, tile_h, bar = 640, 480, 34
    subjects_img = room_subjects + bird_subjects
    cols, rows = 4, (len(subjects_img) + 3) // 4
    montage = Image.new("RGB", (tile_w * cols, (tile_h + bar) * rows), (20, 20, 20))
    draw = ImageDraw.Draw(montage)
    f = font(20)
    for k, s in enumerate(subjects_img):
        n = s["name"]
        res = v3r(n, "正确")
        hits = (
            np.unpackbits(np.frombuffer(res["hits_packed"], dtype=np.uint8))[
                : res["hits_shape"][0] * res["hits_shape"][1]
            ]
            .reshape(res["hits_shape"])
            .astype(bool)
        )
        line_mask = line_arrays[s["line"]] > V2._LINE_BINARY_THRESHOLD
        dx, dy = res["offset"]
        line_sh = V2._shift_zero_fill(line_mask.astype(np.uint8), dx, dy).astype(bool)
        hits_sh = V2._shift_zero_fill(hits.astype(np.uint8), dx, dy).astype(bool)
        base = Image.open(s["path"]).convert("RGB").resize(size, Image.LANCZOS)
        arr = np.array(Image.blend(base, Image.new("RGB", size, (0, 0, 0)), 0.5))
        arr[line_sh] = (255, 60, 60)
        arr[hits_sh] = (40, 255, 40)
        x, y = (k % cols) * tile_w, (k // cols) * (tile_h + bar)
        montage.paste(Image.fromarray(arr).resize((tile_w, tile_h), Image.LANCZOS), (x, y + bar))
        label = f"{n}  v2 {v2s(n, '正确'):.4f}  v3原位 {res['v3_origin']:.4f}  v3配准 {res['v3_best']:.4f} 位移{fmt_off(res['offset'])}"
        draw.text((x + 6, y + 6), label, fill=(255, 255, 255), font=f)
    montage.save(HERE / "montage-叠线命中.png")

    w, h = panels[0][1].size
    dimg = Image.new("RGB", (w * 3 + 20, h + 40), (20, 20, 20))
    d = ImageDraw.Draw(dimg)
    for k, (name, im) in enumerate(panels):
        dimg.paste(im, (k * (w + 10), 40))
        d.text(
            (k * (w + 10) + 6, 6),
            f"{name} line.png  黄＝v2 就进分母  青＝v3 新进分母  灰＝仍不进",
            fill=(255, 255, 255),
            font=font(24),
        )
    dimg.save(HERE / "分母对照.png")

    out["耗时秒"] = round(time.time() - t0, 1)
    (HERE / "结果.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    (HERE / "结果表.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    print(f"完成 {time.time() - t0:.0f}s；表在 结果表.md，数在 结果.json", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
