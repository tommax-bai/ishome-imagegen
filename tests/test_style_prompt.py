"""提示词红线：房间表必须进去、不出字那条必须在、同样输入拼出同样一段话。"""

from __future__ import annotations

import pytest

from imagegen_worker.models import RoomSlot, StyleTemplate
from imagegen_worker.style_prompt import build_prompt, where_in_plan

_TEMPLATE = StyleTemplate(
    template_id="t-test",
    style="warm cream-pink hand-drawn journal",
    composition="strict orthographic top-down 2D",
    negatives=["perspective", "photographic rendering"],
)


def _room(name: str, x: int, y: int) -> RoomSlot:
    return RoomSlot(name=name, mask_index=1, anchor_x_px=x, anchor_y_px=y)


def test_anchor_becomes_a_position_word() -> None:
    # 方位是算出来的不是看图说的：锚点在图幅里的相对坐标 → 九档
    assert where_in_plan(_room("厨房", 50, 50), 900, 900) == "upper-left"
    assert where_in_plan(_room("客厅", 450, 450), 900, 900) == "middle-center"
    assert where_in_plan(_room("次卧", 850, 850), 900, 900) == "lower-right"


def test_room_table_goes_into_the_prompt() -> None:
    """不给房间表，模型只能靠家具猜功能——首跑实测厨房被安到了次卧的位置。"""
    prompt = build_prompt(_TEMPLATE, [_room("厨房", 450, 100), _room("主卧", 100, 800)], 900, 900)

    assert "upper-center is the 厨房" in prompt
    assert "lower-left is the 主卧" in prompt


def test_no_text_clause_always_present() -> None:
    # 字由确定性排版层叠：图像模型逐条排版乱码率相乘，且改一句文案就得重生成整图
    prompt = build_prompt(_TEMPLATE, [_room("客厅", 450, 450)], 900, 900)

    assert "no Chinese characters" in prompt
    assert "Render NO text" in prompt


def test_geometry_source_clause_always_present() -> None:
    # 每张图独立回读母版是架构约束：用上一张继续生成下一张，户型会一路漂
    prompt = build_prompt(_TEMPLATE, [_room("客厅", 450, 450)], 900, 900)

    assert "ONLY architectural geometry source" in prompt
    assert "Do not change the outer silhouette" in prompt


def test_same_input_builds_the_same_prompt() -> None:
    """提示词自己得先是确定的，才谈得上比较两次生成的差别来自模型还是来自我们。"""
    rooms = [_room("客厅", 450, 450), _room("厨房", 450, 100)]

    assert build_prompt(_TEMPLATE, rooms, 900, 900) == build_prompt(_TEMPLATE, rooms, 900, 900)


def test_negatives_are_listed_one_by_one() -> None:
    prompt = build_prompt(_TEMPLATE, [_room("客厅", 450, 450)], 900, 900)

    assert "Avoid: perspective" in prompt
    assert "Avoid: photographic rendering" in prompt


def test_template_rejects_unknown_fields() -> None:
    # 模板是数据；多出来的字段说明模板与代码对不上，当场拒收
    with pytest.raises(ValueError, match="extra"):
        StyleTemplate.model_validate(
            {"templateId": "t", "style": "s", "composition": "c", "roomTable": ["客厅"]}
        )
