"""各向异性缩放配准验证（2026-09-05）：6–22 px 的"整图错位"里有多少是出图非等比缩放造成的？

假设（协调方提出）：万相出图 1168×880 对输入 1280×960 不是等比（宽 1280/1168 = 1.0959，高 960/880 = 1.0909，
比值 1.0046，画幅边缘相当于约 3 px）；尺子把出图 LANCZOS 拉回 1280×960 时这个各向异性就带进来了，
所以"整图错位"可能有一部分是缩放不是模型漂移。

验法：在 v3（窗口半径按模块默认）上，正确配对每张，除平移外再允许 x / y 各自独立缩放，
候选 0.990–1.010 步长 0.002（11×11 = 121 组），看最优缩放落在哪里、残余平移有没有显著变小。

为省时间，缩放候选下的平移窗不是全幅 ±24 而是围绕"只平移时的最优位移"±6 px（`_search_offsets` 的
`center_offset_px`）——±1% 缩放在 640 px 半幅上最多挪 6.4 px，窗够；最优落在窗边上时表里标出来。
只平移的基线仍用全幅 ±24（与主对照矩阵同一条路）。

产出：缩放配准.md / 缩放配准.json。用法（仓根目录）：uv run python _iteration/run-2026-09-05-metric-v3/缩放配准.py
"""

# ruff: noqa: E501  —— 表格行按列写成一行；只放开行宽，其余规则照常
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Any

import numpy as np
from PIL import Image

from imagegen_worker import fidelity_metric as V2
from imagegen_worker import fidelity_metric_v3 as V3

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("duizhao", HERE / "度量对照-v2对v3.py")
assert _spec is not None and _spec.loader is not None
D = importlib.util.module_from_spec(_spec)
sys.modules["duizhao"] = D
_spec.loader.exec_module(D)

SCALE_LO, SCALE_HI, SCALE_STEP = 0.990, 1.010, 0.002
SCALES = [
    round(SCALE_LO + k * SCALE_STEP, 3)
    for k in range(int(round((SCALE_HI - SCALE_LO) / SCALE_STEP)) + 1)
]
LOCAL_RADIUS_PX = 6
EXPECTED_RATIO = (1280 / 1168) / (960 / 880)


def job(subject: dict[str, Any]) -> dict[str, Any]:
    t0 = time.time()
    line_png = D._bytes(D.LINES[subject["line"]])
    result_png = D._bytes(subject["path"])
    line_img = Image.open(pathlib.Path(D.LINES[subject["line"]])).convert("L")
    line_arr = np.asarray(line_img, dtype=np.uint8)
    mask = line_arr > V2._LINE_BINARY_THRESHOLD
    orientation = V3._line_orientation_from_geometry(mask, V3.ORIENTATION_WINDOW_RADIUS_PX)
    result_img = V3._open(result_png, "写实化结果").convert("RGB")
    field = V3._result_edge_field(result_img, line_img.size, V2.EDGE_STRENGTH_PERCENTILE)
    common = {"tolerance_px": V2.TOLERANCE_PX, "angle_tolerance_deg": V2.ANGLE_TOLERANCE_DEG}
    del line_png

    base = V3._search_offsets(
        orientation, field, search_radius_px=V3.REGISTRATION_SEARCH_RADIUS_PX, **common
    )
    dx0, dy0 = base.best_offset
    s0 = base.best_score

    per_scale: dict[str, Any] = {}
    best: dict[str, Any] | None = None
    order = sorted(((sx - 1) ** 2 + (sy - 1) ** 2, sy, sx) for sx in SCALES for sy in SCALES)
    for _, sy, sx in order:
        g = V3._search_offsets(
            orientation,
            field,
            search_radius_px=LOCAL_RADIUS_PX,
            scale_xy=(sx, sy),
            center_offset_px=(dx0, dy0),
            **common,
        )
        bx, by = g.best_offset
        on_window_edge = max(abs(bx - dx0), abs(by - dy0)) == LOCAL_RADIUS_PX
        rec = {
            "sx": sx,
            "sy": sy,
            "dx": bx,
            "dy": by,
            "score": g.best_score,
            "窗边": on_window_edge,
        }
        per_scale[f"{sx:.3f},{sy:.3f}"] = rec
        if best is None or g.best_score > best["score"]:
            best = rec
    assert best is not None
    unit = per_scale["1.000,1.000"]
    return {
        "subject": subject["name"],
        "只平移": {"dx": dx0, "dy": dy0, "score": s0},
        "缩放1局部窗": unit,
        "最优": best,
        "各缩放": per_scale,
        "seconds": round(time.time() - t0, 1),
    }


def main() -> int:
    t0 = time.time()
    subjects = [s for s in D.SUBJECTS]
    print(
        f"缩放候选 {SCALES}（{len(SCALES)}×{len(SCALES)} 组），局部平移窗 ±{LOCAL_RADIUS_PX} px，窗口半径 {V3.ORIENTATION_WINDOW_RADIUS_PX}",
        flush=True,
    )
    rows: list[dict[str, Any]] = []
    with ProcessPoolExecutor() as pool:
        for res in pool.map(job, subjects):
            rows.append(res)
            print(
                f"  {res['subject']}: 只平移 ({res['只平移']['dx']:+d},{res['只平移']['dy']:+d}) {res['只平移']['score']:.4f} → 最优 sx {res['最优']['sx']:.3f} sy {res['最优']['sy']:.3f} ({res['最优']['dx']:+d},{res['最优']['dy']:+d}) {res['最优']['score']:.4f}  {time.time() - t0:.0f}s",
                flush=True,
            )

    md = [
        f"## 各向异性缩放配准（缩放候选 {SCALE_LO:.3f}–{SCALE_HI:.3f} 步长 {SCALE_STEP}，绕画幅中心；平移窗＝只平移最优位移 ±{LOCAL_RADIUS_PX} px；窗口半径 {V3.ORIENTATION_WINDOW_RADIUS_PX}）\n",
        f'假设值：sx/sy = (1280/1168)/(960/880) = {EXPECTED_RATIO:.4f}。"残余平移"＝最优缩放下的位移；"只平移"＝不缩放全幅 ±24 搜出的位移（与主对照矩阵同）。\n',
        "| 出图 | 只平移：位移 / 分数 | 缩放 (1,1) 局部窗分数 | 最优 sx | 最优 sy | sx/sy | 残余平移 (dx,dy) | 最优分数 | Δ分数（对只平移） | \\|平移\\| 只平移 → 残余 | 最优在缩放边界 | 最优在平移窗边 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        b, o = r["最优"], r["只平移"]
        n0 = float(np.hypot(o["dx"], o["dy"]))
        n1 = float(np.hypot(b["dx"], b["dy"]))
        on_scale_edge = b["sx"] in (SCALES[0], SCALES[-1]) or b["sy"] in (SCALES[0], SCALES[-1])
        md.append(
            f"| {r['subject']} | ({o['dx']:+d},{o['dy']:+d}) / {o['score']:.4f} | {r['缩放1局部窗']['score']:.4f} | {b['sx']:.3f} | {b['sy']:.3f} | {b['sx'] / b['sy']:.4f} | "
            f"({b['dx']:+d},{b['dy']:+d}) | {b['score']:.4f} | {b['score'] - o['score']:+.4f} | {n0:.1f} → {n1:.1f} | {'是' if on_scale_edge else '否'} | {'是' if b['窗边'] else '否'} |"
        )
    # 汇总：最优 sx、sy、sx/sy 的分布
    sxs = [r["最优"]["sx"] for r in rows]
    sys_ = [r["最优"]["sy"] for r in rows]
    ratios = [r["最优"]["sx"] / r["最优"]["sy"] for r in rows]
    gains = [r["最优"]["score"] - r["只平移"]["score"] for r in rows]
    md.append("")
    md.append(
        f"最优 sx：中位 {np.median(sxs):.3f}，范围 {min(sxs):.3f}–{max(sxs):.3f}；最优 sy：中位 {np.median(sys_):.3f}，范围 {min(sys_):.3f}–{max(sys_):.3f}；sx/sy：中位 {np.median(ratios):.4f}，范围 {min(ratios):.4f}–{max(ratios):.4f}（假设值 {EXPECTED_RATIO:.4f}）；Δ分数：中位 {np.median(gains):+.4f}，最大 {max(gains):+.4f}。"
    )

    # 分数随缩放的剖面（沿 sy=1 变 sx；沿 sx=1 变 sy），每张图一行——看是尖峰还是平台
    md.append("\n### 分数剖面：sy = 1.000 时随 sx 变化（每格＝该缩放下局部窗最高分）\n")
    md.append("| 出图 | " + " | ".join(f"{s:.3f}" for s in SCALES) + " |")
    md.append("|---|" + "---|" * len(SCALES))
    for r in rows:
        md.append(
            f"| {r['subject']} | "
            + " | ".join(f"{r['各缩放'][f'{s:.3f},1.000']['score']:.4f}" for s in SCALES)
            + " |"
        )
    md.append("\n### 分数剖面：sx = 1.000 时随 sy 变化\n")
    md.append("| 出图 | " + " | ".join(f"{s:.3f}" for s in SCALES) + " |")
    md.append("|---|" + "---|" * len(SCALES))
    for r in rows:
        md.append(
            f"| {r['subject']} | "
            + " | ".join(f"{r['各缩放'][f'1.000,{s:.3f}']['score']:.4f}" for s in SCALES)
            + " |"
        )

    (HERE / "缩放配准.json").write_text(
        json.dumps(
            {
                "缩放候选": SCALES,
                "局部窗": LOCAL_RADIUS_PX,
                "假设比值": EXPECTED_RATIO,
                "逐图": rows,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    (HERE / "缩放配准.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    print(f"完成 {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
