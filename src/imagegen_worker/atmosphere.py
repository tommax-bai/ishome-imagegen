"""风格图生成：母版 + 房间表 + 模板 → 一张图形层。

**每张图独立回读母版**（架构约束，《方案/架构对齐-设计Agent×技术架构》§7.1）：本函数只吃母版，
不吃"上一张风格图"。用上一张继续生成下一张，户型会一路漂。

**产出的是图形层，不含文字**：字由确定性排版层叠上去（待拍板① 的倾向）。模型这边实测听得进
"不要出任何文字"这条。
"""

from __future__ import annotations

import json
from pathlib import Path

from imagegen_worker import image_gateway
from imagegen_worker.models import AtmosphereVisual, RoomSlot, StyleTemplate
from imagegen_worker.style_prompt import build_prompt

ATMOSPHERE_MODEL = "atmosphere-visual.default"
"""逻辑模型名（物理映射在 infra 的网关配置里，换模型不动代码）。"""


class AtmosphereError(Exception):
    """风格图出不来。响亮失败，不给一张"差不多的"图。"""

    def __init__(self, details: list[str]) -> None:
        super().__init__("；".join(details))
        self.details = details


def load_template(path: Path) -> StyleTemplate:
    """读模板。模板是数据（红线：配置只放数据，逻辑归服务）。"""
    with path.open(encoding="utf-8") as f:
        return StyleTemplate.model_validate(json.load(f))


def load_rooms(path: Path) -> list[RoomSlot]:
    """读母版交出来的房间表（名字 + 锚点）。"""
    with path.open(encoding="utf-8") as f:
        return [RoomSlot.model_validate(item) for item in json.load(f)]


def render_atmosphere_visual(
    *,
    master_png: bytes,
    rooms: list[RoomSlot],
    template: StyleTemplate,
    master_width_px: int,
    master_height_px: int,
    api_key: str,
    gateway_url: str = image_gateway.DEFAULT_GATEWAY_URL,
) -> AtmosphereVisual:
    """一次风格图生成。"""
    if not rooms:
        raise AtmosphereError(
            ["房间表是空的：母版上没有字，不给房间表模型只能靠家具猜功能，猜错是必然不是偶然"]
        )
    prompt = build_prompt(template, rooms, master_width_px, master_height_px)
    try:
        image_png, revised = image_gateway.generate_from_image(
            model=ATMOSPHERE_MODEL,
            prompt=prompt,
            source_png=master_png,
            size=template.size,
            api_key=api_key,
            gateway_url=gateway_url,
        )
    except image_gateway.ImageGatewayError as e:
        raise AtmosphereError(e.details) from e
    return AtmosphereVisual(
        image_png=image_png,
        template_id=template.template_id,
        prompt=prompt,
        revised_prompt=revised,
    )
