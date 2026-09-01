"""风格图生成：母版 + 房间表 + 模板 → 一张图形层。

**每张图独立回读母版**（架构约束，《方案/架构对齐-设计Agent×技术架构》§7.1）：本函数只吃母版，
不吃"上一张风格图"。用上一张继续生成下一张，户型会一路漂。

**产出的是图形层，不含文字**：字由确定性排版层叠上去（待拍板① 的倾向）。模型这边实测听得进
"不要出任何文字"这条。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from imagegen_worker import image_gateway
from imagegen_worker.models import (
    AtmosphereVisual,
    LifeObjectSlot,
    RoomAnnotation,
    RoomSlot,
    StyleTemplate,
)
from imagegen_worker.style_prompt import build_prompt

ATMOSPHERE_MODEL = "atmosphere-visual.default"
"""逻辑模型名（物理映射在 infra 的网关配置里，换模型不动代码）。"""


class AtmosphereError(Exception):
    """风格图出不来。响亮失败，不给一张"差不多的"图。"""

    def __init__(self, details: list[str]) -> None:
        super().__init__("；".join(details))
        self.details = details


def master_size_px(master_png: bytes) -> tuple[int, int]:
    """从 PNG 头里读母版宽高。**只为把锚点换算成方位**，不引图像库。

    **两条路共用这一份**：CLI 从磁盘读母版、activity 从私有桶读母版，读完都走这里——
    出图逻辑只有一套，两条路的区别只在母版字节从哪儿来。
    """
    if (
        len(master_png) < 24
        or master_png[:8] != b"\x89PNG\r\n\x1a\n"
        or master_png[12:16] != b"IHDR"
    ):
        raise ValueError("母版不是 PNG（头对不上）：算不出图幅尺寸就换算不出房间方位")
    return (
        int.from_bytes(master_png[16:20], "big"),
        int.from_bytes(master_png[20:24], "big"),
    )


def load_template(path: Path) -> StyleTemplate:
    """读模板。模板是数据（红线：配置只放数据，逻辑归服务）。"""
    with path.open(encoding="utf-8") as f:
        return StyleTemplate.model_validate(json.load(f))


def parse_rooms(payload: bytes) -> list[RoomSlot]:
    """母版交出来的房间表（名字 + 锚点）的字节形态 → 房间槽位。

    **两条路共用这一份**：CLI 从磁盘读那份 JSON、activity 从私有桶读同一份 JSON，
    读完都走这里。产的一侧（render2d）也只写一处字节，两边认的是同一份东西。
    """
    return [RoomSlot.model_validate(item) for item in json.loads(payload.decode("utf-8"))]


def load_rooms(path: Path) -> list[RoomSlot]:
    """从磁盘读房间表（CLI 那条路）。"""
    return parse_rooms(path.read_bytes())


def _check_annotation_and_slot_data(
    *,
    rooms: list[RoomSlot],
    template: StyleTemplate,
    annotations: Sequence[RoomAnnotation],
    life_object_slots: Sequence[LifeObjectSlot],
) -> None:
    """注释与槽位的收货门禁：口径冲突与挂错房间当场拒收，不静默丢。

    静默丢的代价与请求模型里那条相同——派发方以为交代了、执行方没收到，而两边的单测都是绿的。
    """
    problems: list[str] = []
    if annotations and template.room_labels != "handwritten":
        problems.append(
            f"模板 `{template.template_id}` 是零字档（roomLabels=none），收不下要写上图的注释——"
            "注释是字，零字模板的提示词里「不出字」与「写这些字」会打架；要注释就派写字那档模板"
        )
    room_names = {room.name for room in rooms}
    unknown_note_rooms = sorted({note.room for note in annotations} - room_names)
    if unknown_note_rooms:
        problems.append(
            f"注释挂在房间表里没有的房间上：{'、'.join(unknown_note_rooms)}——"
            "没有锚点就没处钉，挂错房间的注释比没有注释更伤信任"
        )
    unknown_slot_rooms = sorted({slot.room for slot in life_object_slots} - room_names)
    if unknown_slot_rooms:
        problems.append(
            f"生活物件槽位挂在房间表里没有的房间上：{'、'.join(unknown_slot_rooms)}——"
            "两侧的房间口径对不上就是接不上头"
        )
    if problems:
        raise AtmosphereError(problems)


def render_atmosphere_visual(
    *,
    master_png: bytes,
    rooms: list[RoomSlot],
    template: StyleTemplate,
    master_width_px: int,
    master_height_px: int,
    api_key: str,
    gateway_url: str = image_gateway.DEFAULT_GATEWAY_URL,
    annotations: Sequence[RoomAnnotation] = (),
    life_object_slots: Sequence[LifeObjectSlot] = (),
) -> AtmosphereVisual:
    """一次风格图生成。注释与槽位是可选的派发数据（不给＝行为与从前逐字节相同）。"""
    if not rooms:
        raise AtmosphereError(
            ["房间表是空的：母版上没有字，不给房间表模型只能靠家具猜功能，猜错是必然不是偶然"]
        )
    _check_annotation_and_slot_data(
        rooms=rooms,
        template=template,
        annotations=annotations,
        life_object_slots=life_object_slots,
    )
    prompt = build_prompt(
        template,
        rooms,
        master_width_px,
        master_height_px,
        annotations=annotations,
        life_object_slots=life_object_slots,
    )
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
