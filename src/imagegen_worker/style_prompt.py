"""模板 + 房间槽位 → 提示词。**纯函数，不做 IO**，单测不需要网关。

模板与实例分开（《方案/第一阶段视觉提案与Prompt说明》§12-1）：模板本体只有风格、构图与禁令，
户型专属的那部分——**哪一间在哪儿**——由母版算出来填进去。

**为什么房间表非填不可**：首跑把母版原样丢给模型、不给房间表，出来的图厨房跑到了次卧的位置。
母版上没有字（它不写字是刻意的），模型只能靠家具猜功能，猜错是必然不是偶然。
"""

from __future__ import annotations

from imagegen_worker.models import RoomSlot, StyleTemplate

_GEOMETRY_CLAUSE = (
    "Input image 1 is the ONLY architectural geometry source. Preserve its exact "
    "floor-plan outer silhouette, wall positions and thicknesses, room boundaries, "
    "door openings and window openings. Do not move, add or remove any wall, door, "
    "window or room. Do not change the outer silhouette."
)
"""几何唯一源那一句。**每张图独立回读母版**是架构约束不是生成技巧——用上一张继续生成下一张，
户型会一路漂（《方案》§3 必须避免的最后一条）。"""

_NO_TEXT_CLAUSE = (
    "Render NO text of any kind: no letters, no Chinese characters, no numbers, "
    "no labels, no captions, no watermarks."
)
"""不出字。字由确定性排版层叠——图像模型逐条排版的乱码率相乘，且改一句文案就得重生成整图。"""

_HANDWRITTEN_LABELS_CLAUSE = (
    "Hand-write each room's Chinese name inside that room, using EXACTLY the characters "
    "given for that room in the room allocation above — no substitutes, no simplification, "
    "no extra characters in the name: {names}. Black handwritten marker, size and angle "
    "varying slightly between rooms, no printed typeface, no label box, no underline, "
    "no pinyin, no English translation."
)
"""**实验档**（`roomLabels: handwritten`，2026-09-01；未裁决，不是新口径）：模型自己写房间名。

名字逐字再复述一遍是刻意的——名字是**我们的数据**（母版那份房间表），不是让模型看家具猜。
上面那句方位表已经说清哪间是哪间，这一句只管"写哪几个字"。

**这一档不禁其他文字**：要不要有手写小注释是风格的事，归模板数据那三段说（业主给的手账风
明确要注释与箭头）。代码这一层只管一件事——**房间名归谁写**。"""

# 九档方位：锚点落在图幅的哪一格。**算出来的，不是看图说的**。
_VERTICAL_BANDS = (("upper", 1 / 3), ("middle", 2 / 3), ("lower", 1.0))
_HORIZONTAL_BANDS = (("left", 1 / 3), ("center", 2 / 3), ("right", 1.0))


def _band(value: float, bands: tuple[tuple[str, float], ...]) -> str:
    for name, upper in bands:
        if value <= upper:
            return name
    return bands[-1][0]


def where_in_plan(room: RoomSlot, width_px: int, height_px: int) -> str:
    """房间在图上的九档方位（upper-left / middle-center / lower-right …）。"""
    vertical = _band(room.anchor_y_px / max(height_px, 1), _VERTICAL_BANDS)
    horizontal = _band(room.anchor_x_px / max(width_px, 1), _HORIZONTAL_BANDS)
    return f"{vertical}-{horizontal}"


def build_prompt(
    template: StyleTemplate, rooms: list[RoomSlot], width_px: int, height_px: int
) -> str:
    """拼出发给图像模型的那段话。顺序固定：几何 → 风格 → 构图 → 房间表 → 文字层 → 禁令。

    顺序固定是为了**同样的输入拼出同样的一段话**——提示词自己得先是确定的，
    才谈得上比较两次生成的差别来自模型还是来自我们。

    **文字层那一句只有两种，由模板的 `roomLabels` 选**（默认 `none`＝一个字都不出）：
    没有房间表时两档都退回"不出字"——名字都拿不到，更谈不上让模型照着写。
    """
    lines = [
        "Use case: style-transfer of an architectural floor plan.",
        _GEOMETRY_CLAUSE,
        f"Style: {template.style}",
        f"Composition: {template.composition}",
    ]
    if rooms:
        # 房间表按方位念，不按遮罩索引念——索引是内部编号，对模型没有意义
        listed = "; ".join(
            f"{where_in_plan(room, width_px, height_px)} is the {room.name}" for room in rooms
        )
        lines.append(
            "Room allocation (follow it exactly; draw furniture appropriate to each room's "
            f"function and nothing else): {listed}."
        )
    if rooms and template.room_labels == "handwritten":
        lines.append(_HANDWRITTEN_LABELS_CLAUSE.format(names="、".join(r.name for r in rooms)))
    else:
        lines.append(_NO_TEXT_CLAUSE)
    lines.extend(f"Avoid: {item}" for item in template.negatives)
    return "\n".join(lines)
