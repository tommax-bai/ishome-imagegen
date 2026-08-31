"""CLI：母版 + 房间表 + 模板 → 一张风格图。

    imagegen --master out/plan-master.png --rooms out/rooms.json \
             --template templates/cream-journal.json -o style.png

工具形态先行（同母版、同渲染层）：先能画出一张来，再谈它在编排里怎么被调——
**接进 activity 的时点写死＝派发链路接通时**。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from imagegen_worker.atmosphere import (
    AtmosphereError,
    load_rooms,
    load_template,
    render_atmosphere_visual,
)
from imagegen_worker.image_gateway import DEFAULT_GATEWAY_URL


def _master_size(path: Path) -> tuple[int, int]:
    """从 PNG 头里读宽高。**只为把锚点换算成方位**，不引图像库。"""
    header = path.read_bytes()[:33]
    if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"不是 PNG：{path}")
    return (
        int.from_bytes(header[16:20], "big"),
        int.from_bytes(header[20:24], "big"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="imagegen",
        description="风格图生成：母版 + 房间表 + 模板 → 一张图形层（不含文字）",
    )
    parser.add_argument("--master", required=True, type=Path, help="母版 PNG（唯一几何来源）")
    parser.add_argument("--rooms", required=True, type=Path, help="母版交出来的房间表 JSON")
    parser.add_argument("--template", required=True, type=Path, help="风格模板 JSON")
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
        width_px, height_px = _master_size(args.master)
        rooms = load_rooms(args.rooms)
        template = load_template(args.template)
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
