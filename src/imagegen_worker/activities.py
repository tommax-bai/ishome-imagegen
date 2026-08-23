"""Temporal activities：所有 IO 与重计算收口在此。

注册名唯一真源：ishome-contracts `activities/registry.md`，**只增不改**——改注册名
会破坏历史 workflow 重放，等同于改线上协议；新增走 contracts 仓 PR 评审。
命名规则（规范 §2.4）：注册名 = kebab-case 显式声明；函数名 = 同词 snake_case
动词前置。
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from temporalio import activity

from imagegen_worker.models import AtmosphereVisualRequest, RealismPassRequest

ActivityResult = dict[str, Any]


@activity.defn(name="atmosphere-visual")
async def generate_atmosphere_visual(request: AtmosphereVisualRequest) -> ActivityResult:
    """风格化交付图生成（模板库驱动，固定遮罩；每张独立回读母版）。"""
    raise NotImplementedError


@activity.defn(name="realism-pass")
async def apply_realism_pass(request: RealismPassRequest) -> ActivityResult:
    """生成式写实化（工厂效果图同用）。"""
    raise NotImplementedError


ACTIVITY_REGISTRY: dict[str, Callable[..., Coroutine[Any, Any, ActivityResult]]] = {
    "atmosphere-visual": generate_atmosphere_visual,
    "realism-pass": apply_realism_pass,
}
"""注册名 → 实现。键与 contracts 注册表逐字一致（tests/test_activity_registry.py 断言）。"""
