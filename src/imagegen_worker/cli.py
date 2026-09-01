"""CLI：母版 + 房间表 + 模板 → 一张风格图。

    imagegen --master out/plan-master.png --rooms out/rooms.json \
             --template templates/cream-journal.json -o style.png

**CLI 不废**（服务已建立，2026-08-31）：它是本地迭代的入口——换模板、看一张图长什么样走它，
不必起 Temporal、也不碰私有桶。与 activity 那条路**共用同一份纯库代码**（`atmosphere` /
`style_prompt` / `image_gateway`），两条路的区别只在母版字节从哪儿来；分界由 import-linter
锁死（`cli` 看不见 `activities`）——从它能看见起，"本地改模板不需要桶凭证"就只是一句承诺
而不是结构。
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
from imagegen_worker.models import LifeObjectSlot, RoomAnnotation


def main(argv: list[str] | None = None) -> int:
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

    api_key = os.environ.get("LITELLM_API_KEY", "")
    if not api_key:
        print(
            "没有 LITELLM_API_KEY：出图要经网关，凭证放 ~/.ishome/llm-local.env（不入库）",
            file=sys.stderr,
        )
        return 2

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
