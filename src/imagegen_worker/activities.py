"""Temporal activities：所有 IO 与重计算收口在此。

注册名唯一真源：ishome-contracts `activities/registry.md`，**只增不改**——改注册名
会破坏历史 workflow 重放，等同于改线上协议；新增走 contracts 仓 PR 评审。
命名规则（规范 §2.4）：注册名 = kebab-case 显式声明；函数名 = 同词 snake_case
动词前置。

**imagegen 至此成服务**（2026-08-31）。原形态是"纯库 + CLI，接进 activity 的时点写死＝
派发链路接通时"——母版进了私有桶、图要送到业主手上，触发条件即此。形态照渲染层
（reportrender）的先例做：实现件是类、进程级依赖（私有桶、模板库、网关凭证）由组合根
`worker` 注入并在**起进程时**当场校验，入参是**不透明字典**而不是本仓模型。

**CLI 不废**：它是本地迭代的入口——换模板、看一张图长什么样走它，不必起 Temporal、也不碰桶。
两条路共用同一份纯库代码（`atmosphere` / `style_prompt` / `image_gateway`），**没有第二套
出图逻辑**；分界由 import-linter 锁死（`cli` 看不见 `activities`）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError
from temporalio import activity

from imagegen_worker.activity_log import logged_activity
from imagegen_worker.atmosphere import (
    AtmosphereError,
    master_size_px,
    parse_rooms,
    render_atmosphere_visual,
)
from imagegen_worker.image_store import ImageStoreError, OssImageStore, image_content_type_of
from imagegen_worker.models import (
    AtmosphereVisualRequest,
    RealismPassRequest,
    RealismStyleTemplate,
    StyleTemplate,
)
from imagegen_worker.realism import (
    RealismBackend,
    RealismError,
    RealismGate,
    render_realism_visual,
)

ActivityResult = dict[str, Any]

ACTIVITY_ATMOSPHERE_VISUAL = "atmosphere-visual"
ACTIVITY_REALISM_PASS = "realism-pass"
"""contracts 注册名（#5 / #8）。字符串在此各声明一次，worker 与守门测试都引它。"""


class AtmosphereVisualGenerator:
    """风格图 activity 的实现件，依赖由组合根（worker）注入。

    做成类而不是自由函数，是因为它要用三样**进程级**的东西：私有桶的连接、模板库、网关凭证。
    三样都该在**起进程时**装好并当场校验——缺配置要在 worker 起不来的时候就知道，
    不是等第一张图出到一半才发现存不进去（同渲染层的出册件）。
    """

    def __init__(
        self,
        store: OssImageStore,
        templates: dict[str, StyleTemplate],
        api_key: str,
        gateway_url: str,
    ) -> None:
        self._store = store
        self._templates = templates
        self._api_key = api_key
        self._gateway_url = gateway_url

    @activity.defn(name=ACTIVITY_ATMOSPHERE_VISUAL)
    @logged_activity
    async def generate_atmosphere_visual(self, request: dict[str, Any]) -> ActivityResult:
        """母版（桶里）+ 房间表 + 模板 → 一张风格图 → 写回同一前缀，返回**对象键**。

        **每张图独立回读母版**（架构约束）：本 activity 只吃母版，不吃"上一张风格图"——
        用上一张接着生成下一张，户型会一路漂。

        返回对象键不返回本地路径：出图这台机器上的路径，对下游没有任何意义。
        """
        try:
            parsed = AtmosphereVisualRequest.model_validate(request)
        except ValidationError as e:
            return _failed("gate-bad-input", f"入参解析失败：{e}")

        template = self._templates.get(parsed.template_id)
        if template is None:
            known = "、".join(sorted(self._templates)) or "（一个都没装上）"
            return _failed(
                "gate-unknown-template", f"没有模板 `{parsed.template_id}`：本进程装了 {known}"
            )

        try:
            # 取这两件顺带把键的形态判了（键不合约定就不发请求）——**要在花钱出图之前判**：
            # 键不对的话，图出来了也没有地方放。房间表还要与母版同前缀，否则就是拿甲的
            # 房间表去画乙的户型。
            master_png = await asyncio.to_thread(self._store.get_master, parsed.master_object_key)
            room_anchors_json = await asyncio.to_thread(
                self._store.get_room_anchors,
                parsed.master_object_key,
                parsed.room_anchors_object_key,
            )
        except ImageStoreError as e:
            return _failed_many("image-store-failed", e.details)

        try:
            master_width_px, master_height_px = master_size_px(master_png)
        except ValueError as e:
            return _failed("gate-bad-master", f"{e}（键 {parsed.master_object_key}）")

        try:
            rooms = parse_rooms(room_anchors_json)
        except (ValueError, TypeError) as e:
            return _failed(
                "gate-bad-room-anchors",
                f"房间表读不成（键 {parsed.room_anchors_object_key}）：{e}",
            )

        try:
            # **出图是几十秒的阻塞调用**（网关超时上限 300 秒）：直接在事件循环里跑，
            # 同一个 worker 上别的 activity 全程排队。丢进线程里跑。
            visual = await asyncio.to_thread(
                render_atmosphere_visual,
                master_png=master_png,
                rooms=rooms,
                template=template,
                master_width_px=master_width_px,
                master_height_px=master_height_px,
                api_key=self._api_key,
                gateway_url=self._gateway_url,
                annotations=parsed.annotations,
                life_object_slots=parsed.life_object_slots,
            )
        except AtmosphereError as e:
            # 房间表空、输入图没送到模型（静默退化成文生图）、注释/槽位与房间表对不上——
            # 几道门禁都从这儿出来，**逐条回报，不给一张差不多的图**。
            # 不上抛异常是因为这几类重试一万次也是同一个结果，而每重试
            # 一次都要再花一次出图的钱：要不要重试由编排侧显式决定。
            return _failed_many("atmosphere-failed", e.details)

        try:
            image_object_key = await asyncio.to_thread(
                self._store.put_atmosphere_visual,
                parsed.master_object_key,
                template.template_id,
                visual.image_png,
            )
        except ImageStoreError as e:
            # 图出得再好、落不了地也按失败回报——回一个指向空气的键，下游会拿它去发给业主。
            return _failed_many("image-store-failed", e.details)

        return {
            "verdict": "ok",
            # 叫 `image_object_key` 不叫 `image_key`：后者是飞书的方言（渠道那边的图片标识），
            # 撞了名迟早有人把这两样接反。
            "image_object_key": image_object_key,
            "bucket": self._store.bucket_name,
            "master_object_key": parsed.master_object_key,
            "room_anchors_object_key": parsed.room_anchors_object_key,
            "template_id": template.template_id,
            "room_count": len(rooms),
            "image_size_bytes": len(visual.image_png),
            # 键的扩展名与这个头同源同判、都随字节走（裁决 2026-09-02"标签跟着内容走"）：
            # 下游要发这张图时不必再去问一次存储。
            "content_type": image_content_type_of(visual.image_png),
            # 提示词随回执留档：**将来要能回答"这张图当时是怎么要出来的"**。不另写一个
            # `*.prompt.txt` 对象——多一条键就多一处跨仓要对齐的副本，而这条回执本来就会
            # 留在 Temporal 历史里。
            "prompt": visual.prompt,
            "revised_prompt": visual.revised_prompt,
        }


class RealismPassRenderer:
    """写实化 activity 的实现件，依赖由组合根（worker）注入：私有桶、写实风格模板库、后端、门禁。

    存根落地的触发条件（"render3d 的 `base-render` 出得来第一张底渲"）2026-09-01 已成立；
    换到控制通道（几何由我们的线稿定）的三判据 2026-09-02 已过，本件按那条通路的契约实装。
    形态与 `AtmosphereVisualGenerator` 相同：取键 → 纯库 → 写桶 → 返回键与自证数；
    入参是不透明字典；**不知用户是谁**。
    """

    def __init__(
        self,
        store: OssImageStore,
        templates: dict[str, RealismStyleTemplate],
        backend: RealismBackend,
        gate: RealismGate,
    ) -> None:
        self._store = store
        self._templates = templates
        self._backend = backend
        self._gate = gate

    @activity.defn(name=ACTIVITY_REALISM_PASS)
    @logged_activity
    async def apply_realism_pass(self, request: dict[str, Any]) -> ActivityResult:
        """线稿（桶里）+ 风格模板 + 视角 → 写实图 → 量分 → 写回同一前缀，返回**对象键**与自证数。

        **几何由线稿定，不由模型定**：控制稿是线稿逐像素反色，一次重采样都不做。
        **没有档位**、**默认无陈设**、**产物键由本仓从源键派生**（三条口径见
        `models.RealismPassRequest`）。

        门禁**只做两件**：算分、与配置里的下限比。没配下限＝只记录不判（回执里照样带分数）；
        配了下限、不过线＝按 failed 回报并带分数与原因，**图不进桶**（同键会盖掉上一张过线的）。
        不重出——重出几次、换不换 seed 由编排定。
        """
        try:
            parsed = RealismPassRequest.model_validate(request)
        except ValidationError as e:
            return _failed("gate-bad-input", f"入参解析失败：{e}")

        template = self._templates.get(parsed.style_template_id)
        if template is None:
            known = "、".join(sorted(self._templates)) or "（一个都没装上）"
            return _failed(
                "gate-unknown-style-template",
                f"没有写实风格模板 `{parsed.style_template_id}`：本进程装了 {known}",
            )

        source_object_key = parsed.source_object_key
        try:
            # 取线稿顺带把键的形态判了（当不了派生物前缀来源的键不发请求）——要在花钱出图之前判。
            line_png = await asyncio.to_thread(self._store.get_control_source, source_object_key)
        except ImageStoreError as e:
            return _failed_many("image-store-failed", e.details)

        try:
            # 出图是几十秒的阻塞调用（真跑 18–95 s）：丢进线程里跑，不占事件循环。
            visual = await asyncio.to_thread(
                render_realism_visual,
                line_png=line_png,
                template=template,
                view_kind=parsed.view_kind,
                seed=parsed.seed,
                backend=self._backend,
            )
        except RealismError as e:
            # 线稿解不成图、网关拒绝、回执没有图——逐条回报；要不要重试由编排侧显式决定，
            # 每重试一次都要再花一次出图的钱。
            return _failed_many("realism-failed", e.details)

        try:
            # 量分是纯 CPU（1280×960 上几秒），同样不占事件循环。尺子认原始线稿，不认反色稿。
            verdict = await asyncio.to_thread(self._gate.judge, line_png, visual.image_bytes)
        except RealismError as e:
            return _failed_many("fidelity-gate-failed", e.details)

        evidence: dict[str, Any] = {
            "source_object_key": source_object_key,
            "camera_id": parsed.camera_id,
            "style_template_id": template.template_id,
            "view_kind": parsed.view_kind,
            # 自证数：这张图是谁出的、能不能复现、几何锁得怎样、花了多久、当时要的是哪段话
            "backend_name": visual.output.backend_name,
            "seed": visual.output.seed,
            "fidelity_score": verdict.fidelity_score,
            "elapsed_seconds": visual.output.elapsed_seconds,
            "prompt_sha256": visual.prompt_sha256,
            "prompt": visual.prompt,
            "gate": verdict.as_dict(),
        }
        if verdict.judged and not verdict.passed:
            return {
                **_failed("fidelity-gate-failed", verdict.reason or "不过线"),
                **evidence,
            }

        try:
            image_object_key = await asyncio.to_thread(
                self._store.put_realism_visual,
                source_object_key,
                parsed.camera_id,
                template.template_id,
                visual.image_bytes,
            )
        except ImageStoreError as e:
            # 图出得再好、落不了地也按失败回报——回一个指向空气的键，下游会拿它去发给业主。
            return _failed_many("image-store-failed", e.details)

        return {
            "verdict": "ok",
            "image_object_key": image_object_key,
            "bucket": self._store.bucket_name,
            "image_size_bytes": len(visual.image_bytes),
            "content_type": image_content_type_of(visual.image_bytes),
            **evidence,
        }


def _failed(check: str, detail: str) -> ActivityResult:
    return {"verdict": "failed", "violations": [{"check": check, "detail": detail}]}


def _failed_many(check: str, details: list[str]) -> ActivityResult:
    return {
        "verdict": "failed",
        "violations": [{"check": check, "detail": detail} for detail in details],
    }


def activity_registry(
    generator: AtmosphereVisualGenerator, renderer: RealismPassRenderer
) -> dict[str, Callable[..., Any]]:
    """本仓承接的 activity 全集（队列 `imagegen-activities`）。

    键与 contracts 注册表逐字一致（tests/test_activity_registry.py 断言）。
    """
    return {
        ACTIVITY_ATMOSPHERE_VISUAL: generator.generate_atmosphere_visual,
        ACTIVITY_REALISM_PASS: renderer.apply_realism_pass,
    }
