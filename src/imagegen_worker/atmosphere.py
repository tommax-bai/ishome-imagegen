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


_ANNOTATION_ENTITY_NOUNS: tuple[tuple[str, str], ...] = (
    ("床头柜", "bedside table"),
    ("洗衣机", "washing machine"),
    ("洗手池", "basin"),
    ("书桌", "desk"),
    ("餐桌", "dining table"),
    ("鞋柜", "shoe cabinet"),
    ("衣柜", "wardrobe"),
    ("马桶", "toilet"),
    ("淋浴", "shower"),
    ("沙发", "sofa"),
    ("玩具", "toy"),
    ("床", "bed"),
    ("桌", "table"),
    ("椅", "chair"),
    ("灯", "lamp"),
)
"""注释里数得出的实体名词 → 槽位清单里对应的英文词。**字面匹配用，不上模型**。

门禁数据不是知识库：只收"中文名词 → 清单英文用词"对应唯一、字面判得动的词——单字"柜"
就没进来（衣柜/鞋柜/床头柜各自成词在表里，剩下的"柜"对 cabinet/wardrobe/cupboard 哪个都
不唯一，字面匹配会把对的数据拦下来）。结构件（飘窗/窗/门/墙）也不进来：它们由母版几何
保证，不归槽位清单管。匹配最长优先、命中段掩掉再继续——"床头柜"不再触发"床"。"""


def _entity_nouns_in(text: str) -> list[tuple[str, str]]:
    """注释文本里出现的实体名词。最长优先，命中段掩掉——"书桌"命中后不再触发"桌"。"""
    found: list[tuple[str, str]] = []
    masked = text
    for noun, keyword in sorted(_ANNOTATION_ENTITY_NOUNS, key=lambda pair: -len(pair[0])):
        if noun in masked:
            found.append((noun, keyword))
            masked = masked.replace(noun, "＊")
    return found


def _check_annotation_and_slot_data(
    *,
    rooms: list[RoomSlot],
    template: StyleTemplate,
    annotations: Sequence[RoomAnnotation],
    life_object_slots: Sequence[LifeObjectSlot],
) -> None:
    """注释与槽位的收货门禁：口径冲突、挂错房间、清单不全、两半打架，当场拒收不静默丢。

    静默丢的代价与请求模型里那条相同——派发方以为交代了、执行方没收到，而两边的单测都是绿的。
    四道检查：①零字模板收不下注释；②注释/槽位不许挂在房间表外；③槽位给了就得给全、
    一间一份（全集口径，2026-09-01 晚）；④注释提到的实体必须在那间房的清单里——
    上一轮小孩房注释写着"床和书桌"、清单只有玩具架与书桌，两跑都没画床，这一道就是防它再犯。
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
    slot_rooms = [slot.room for slot in life_object_slots]
    unknown_slot_rooms = sorted(set(slot_rooms) - room_names)
    if unknown_slot_rooms:
        problems.append(
            f"物件槽位挂在房间表里没有的房间上：{'、'.join(unknown_slot_rooms)}——"
            "两侧的房间口径对不上就是接不上头"
        )
    duplicate_slot_rooms = sorted({room for room in slot_rooms if slot_rooms.count(room) > 1})
    if duplicate_slot_rooms:
        problems.append(
            f"同一间房给了两份槽位清单：{'、'.join(duplicate_slot_rooms)}——"
            "全集口径下一间房只有一份全集，两份就说不清哪份算数"
        )
    if life_object_slots:
        uncovered_rooms = sorted(room_names - set(slot_rooms))
        if uncovered_rooms:
            problems.append(
                f"槽位给了却没给全，这些房间没有清单：{'、'.join(uncovered_rooms)}——"
                "全集口径下没清单的房间就是让模型猜着画（上一轮无槽位的卫生间跑出近乎空房、"
                "连马桶都没有）；槽位要么一间不给（整张中性画法），要么每间都给"
            )
    inventory_by_room = {slot.room: "; ".join(slot.objects).lower() for slot in life_object_slots}
    for note in annotations:
        if note.room not in room_names:
            continue  # 房间本身就挂错了，上面已报过，不再往下追实体
        nouns = _entity_nouns_in(note.text)
        if not nouns:
            continue
        inventory = inventory_by_room.get(note.room)
        if inventory is None:
            listed = "、".join(noun for noun, _ in nouns)
            problems.append(
                f"{note.room}的注释提到实体（{listed}），这间房却没有槽位清单——"
                "注释说的东西没人保证画出来，正是上一轮「注释写床、清单没床」两半打架的形态"
            )
            continue
        problems.extend(
            f"{note.room}的注释写着「{noun}」，它的槽位清单里却没有对应物（{keyword}）——"
            "同一份派发数据两半打架：箭头会指着一样画不出来的东西"
            for noun, keyword in nouns
            if keyword not in inventory
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
