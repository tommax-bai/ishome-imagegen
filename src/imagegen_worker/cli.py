"""CLI：母版 + 房间表 + 模板 → 一张风格图；`realism` 子命令：线稿 + 风格 + 视角 → 一张写实图。

    imagegen --master out/plan-master.png --rooms out/rooms.json \
             --template templates/cream-journal.json -o style.png
    imagegen realism --line out/cam-bird-dollhouse/line.png --style modern-minimal --view bird
             [--seed 12345] [--gateway http://127.0.0.1:4000] -o realism.png

**CLI 不废**（服务已建立，2026-08-31）：它是本地迭代的入口——换模板、看一张图长什么样走它，
不必起 Temporal、也不碰私有桶。与 activity 那条路**共用同一份纯库代码**（`atmosphere` /
`style_prompt` / `image_gateway`；写实化是 `realism` / `fidelity_metric`），两条路的区别只在
源图字节从哪儿来；分界由 import-linter 锁死（`cli` 看不见 `activities`）——从它能看见起，
"本地改模板不需要桶凭证"就只是一句承诺而不是结构。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from imagegen_worker.atmosphere import (
    AtmosphereError,
    load_rooms,
    load_template,
    master_size_px,
    render_atmosphere_visual,
)
from imagegen_worker.image_gateway import DEFAULT_GATEWAY_URL
from imagegen_worker.models import LifeObjectSlot, RealismStyleTemplate, RoomAnnotation
from imagegen_worker.realism import (
    RealismError,
    RealismGate,
    backend_from_config,
    backend_name_from_env,
    render_realism_visual,
)

REALISM_SUBCOMMAND = "realism"
TEMPLATES_DIR_ENV = "ISHOME_IMAGEGEN_TEMPLATES_DIR"

EXIT_BAD_INPUT = 2
EXIT_GENERATION_FAILED = 3
EXIT_GATE_FAILED = 4


def main(argv: list[str] | None = None) -> int:
    args_list = sys.argv[1:] if argv is None else argv
    if args_list[:1] == [REALISM_SUBCOMMAND]:
        # 子命令只在这里分叉：风格图那条路的参数与行为一个字不动
        return realism_main(args_list[1:])
    return _atmosphere_main(args_list)


def _api_key() -> str | None:
    api_key = os.environ.get("LITELLM_API_KEY", "")
    if not api_key:
        print(
            "没有 LITELLM_API_KEY：出图要经网关，凭证放 ~/.ishome/llm-local.env（不入库）",
            file=sys.stderr,
        )
        return None
    return api_key


def realism_main(argv: list[str]) -> int:
    """写实化：一份 render3d 的 line.png + 风格 id + 视角 → 一张图；同时打印分数与自证数。

    产物：`-o` 那张图、旁边的 `.sketch.png`（真正送出去的控制稿）与 `.prompt.txt`（真正发出去
    的话）——真跑时要能回答"这张图当时是怎么要出来的"。门禁下限只从配置来
    （`ISHOME_REALISM_MIN_FIDELITY_SCORE`，不配＝只记录不判）；不过线时图照样写出来看、退出码 4。
    """
    parser = argparse.ArgumentParser(
        prog=f"imagegen {REALISM_SUBCOMMAND}",
        description="写实化：线稿（黑底白线）+ 风格模板 + 视角 → 一张写实图，默认无陈设",
    )
    parser.add_argument(
        "--line", required=True, type=Path, help="render3d 的 line.png（唯一几何来源）"
    )
    parser.add_argument(
        "--style", required=True, help="写实风格模板 id（templates/realism/<id>.json）"
    )
    parser.add_argument(
        "--view", required=True, choices=["bird", "room"], help="视角：揭顶鸟瞰 / 室内机位"
    )
    parser.add_argument("--seed", type=int, default=None, help="送给后端的 seed；不给＝不可复现")
    parser.add_argument(
        "--gateway", default=os.environ.get("LITELLM_BASE_URL", DEFAULT_GATEWAY_URL)
    )
    parser.add_argument(
        "--templates-dir",
        type=Path,
        default=Path(os.environ.get(TEMPLATES_DIR_ENV, "").strip() or "templates"),
        help="模板目录（写实模板在其 realism/ 子目录）；"
        "缺省取 ISHOME_IMAGEGEN_TEMPLATES_DIR 或 ./templates",
    )
    parser.add_argument("-o", "--out", type=Path, default=Path("realism.png"))
    args = parser.parse_args(argv)

    api_key = _api_key()
    if api_key is None:
        return EXIT_BAD_INPUT

    template_path = args.templates_dir / "realism" / f"{args.style}.json"
    try:
        line_png = args.line.read_bytes()
        template = RealismStyleTemplate.model_validate(
            json.loads(template_path.read_text(encoding="utf-8"))
        )
        if template.template_id != args.style:
            raise ValueError(
                f"模板文件名与 templateId 对不上：{template_path.name} vs `{template.template_id}`"
            )
    except (OSError, ValueError, ValidationError) as e:
        print(f"读输入失败：{e}", file=sys.stderr)
        return EXIT_BAD_INPUT

    try:
        backend = backend_from_config(
            backend_name_from_env(), api_key=api_key, gateway_url=args.gateway
        )
        gate = RealismGate.from_env()
        visual = render_realism_visual(
            line_png=line_png,
            template=template,
            view_kind=args.view,
            seed=args.seed,
            backend=backend,
        )
    except RealismError as e:
        print("写实图出不来（fail loud，不给一张差不多的图）：", file=sys.stderr)
        for line in e.details:
            print(f"  - {line}", file=sys.stderr)
        return EXIT_GENERATION_FAILED

    args.out.write_bytes(visual.image_bytes)
    args.out.with_suffix(".sketch.png").write_bytes(visual.control_sketch_png)
    args.out.with_suffix(".prompt.txt").write_text(visual.prompt, encoding="utf-8")
    print(
        f"写实图已出：{args.out}（风格 {template.template_id}，视角 {args.view}，"
        f"{len(visual.image_bytes)} 字节）"
    )
    print(
        f"控制稿留档：{args.out.with_suffix('.sketch.png')}；"
        f"提示词留档：{args.out.with_suffix('.prompt.txt')}"
    )

    try:
        verdict = gate.judge(line_png, visual.image_bytes)
    except RealismError as e:
        print("分数量不出来：", "；".join(e.details), file=sys.stderr)
        return EXIT_GATE_FAILED

    print(f"backend_name={visual.output.backend_name} seed={visual.output.seed}")
    print(
        f"elapsed_seconds={visual.output.elapsed_seconds:.1f} prompt_sha256={visual.prompt_sha256}"
    )
    print(f"fidelity_score={verdict.fidelity_score:.4f}（结构边重合度，只能同输出形态横向比）")
    if verdict.v3 is not None:
        v3 = verdict.v3
        print(
            f"fidelity_v3_at_origin={v3.score_at_origin:.4f} "
            f"fidelity_v3_registered={v3.score_registered:.4f} "
            f"registration=sx={v3.sx:.3f} sy={v3.sy:.3f} dx={v3.dx:+d} dy={v3.dy:+d}（只记录不判）"
        )
    else:
        print(f"fidelity_v3=量不出来：{verdict.v3_error}（只记录，不影响出图）")
    if not verdict.judged:
        print("gate=只记录不判（没配 ISHOME_REALISM_MIN_FIDELITY_SCORE）")
        return 0
    if verdict.passed:
        print(f"gate=过线（下限 {verdict.min_fidelity_score}）")
        return 0
    print(f"gate=不过线：{verdict.reason}", file=sys.stderr)
    return EXIT_GATE_FAILED


def _atmosphere_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="imagegen",
        description="风格图生成：母版 + 房间表 + 模板 → 一张图形层（不含文字）",
    )
    parser.add_argument("--master", required=True, type=Path, help="母版 PNG（唯一几何来源）")
    parser.add_argument("--rooms", required=True, type=Path, help="母版交出来的房间表 JSON")
    parser.add_argument("--template", required=True, type=Path, help="风格模板 JSON")
    parser.add_argument(
        "--annotations",
        type=Path,
        default=None,
        help="要写上图的注释 JSON（[{room, text}]，内容我们给、模型只画字；只有写字档模板收）",
    )
    parser.add_argument(
        "--life-objects",
        type=Path,
        default=None,
        help="逐间物件槽位 JSON（[{room, objects}]，清单＝全集：功能家具＋生活物件；"
        "给了就得给全，一间都不给＝整张中性画法）",
    )
    parser.add_argument("-o", "--out", type=Path, default=Path("style.png"))
    parser.add_argument(
        "--gateway", default=os.environ.get("LITELLM_BASE_URL", DEFAULT_GATEWAY_URL)
    )
    args = parser.parse_args(argv)

    api_key = _api_key()
    if api_key is None:
        return EXIT_BAD_INPUT

    try:
        master_png = args.master.read_bytes()
        width_px, height_px = master_size_px(master_png)
        rooms = load_rooms(args.rooms)
        template = load_template(args.template)
        annotations = (
            [
                RoomAnnotation.model_validate(item)
                for item in json.loads(args.annotations.read_text(encoding="utf-8"))
            ]
            if args.annotations
            else []
        )
        life_object_slots = (
            [
                LifeObjectSlot.model_validate(item)
                for item in json.loads(args.life_objects.read_text(encoding="utf-8"))
            ]
            if args.life_objects
            else []
        )
    except (OSError, ValueError, ValidationError) as e:
        print(f"读输入失败：{e}", file=sys.stderr)
        return 2

    try:
        visual = render_atmosphere_visual(
            master_png=master_png,
            rooms=rooms,
            template=template,
            master_width_px=width_px,
            master_height_px=height_px,
            api_key=api_key,
            gateway_url=args.gateway,
            annotations=annotations,
            life_object_slots=life_object_slots,
        )
    except AtmosphereError as e:
        print("风格图出不来（fail loud，不给一张差不多的图）：", file=sys.stderr)
        for line in e.details:
            print(f"  - {line}", file=sys.stderr)
        return 3

    args.out.write_bytes(visual.image_png)
    args.out.with_suffix(".prompt.txt").write_text(visual.prompt, encoding="utf-8")
    print(f"风格图已出：{args.out}（模板 {visual.template_id}，{len(visual.image_png)} 字节）")
    print(f"提示词留档：{args.out.with_suffix('.prompt.txt')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
