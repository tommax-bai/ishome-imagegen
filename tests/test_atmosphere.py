"""风格图编排红线：没有房间表不出图；网关说输入图没送到即整张失败。"""

from __future__ import annotations

import pytest

from imagegen_worker import atmosphere, image_gateway
from imagegen_worker.models import RoomSlot, StyleTemplate

_TEMPLATE = StyleTemplate(template_id="t", style="s", composition="c")
_ROOMS = [RoomSlot(name="客厅", mask_index=1, anchor_x_px=100, anchor_y_px=100)]


def test_no_room_table_fails_loud() -> None:
    with pytest.raises(atmosphere.AtmosphereError, match="房间表是空的"):
        atmosphere.render_atmosphere_visual(
            master_png=b"png",
            rooms=[],
            template=_TEMPLATE,
            master_width_px=900,
            master_height_px=900,
            api_key="k",
        )


def test_gateway_failure_surfaces_as_atmosphere_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """图生图静默退化成文生图是这条线上最贵的失败：调用成功、图与母版无关。"""

    def _boom(**_: object) -> tuple[bytes, str]:
        raise image_gateway.ImageGatewayError(["输入图没送到模型（回执 input_images=None）"])

    monkeypatch.setattr(image_gateway, "generate_from_image", _boom)

    with pytest.raises(atmosphere.AtmosphereError, match="输入图没送到模型"):
        atmosphere.render_atmosphere_visual(
            master_png=b"png",
            rooms=_ROOMS,
            template=_TEMPLATE,
            master_width_px=900,
            master_height_px=900,
            api_key="k",
        )


def test_empty_master_never_reaches_the_model() -> None:
    with pytest.raises(image_gateway.ImageGatewayError, match="源图是空的"):
        image_gateway.generate_from_image(
            model="m", prompt="p", source_png=b"", size="2K", api_key="k"
        )
