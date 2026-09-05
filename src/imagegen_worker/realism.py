"""写实化（realism-pass）纯库：线稿 → 控制稿 + 提示词 → 后端出图 → 出口门禁量分。**不做桶 IO。**

**几何由我们的线稿定，不由模型定**（《评审/控制图通路调研-2026-09-02.md》§一）：写实化从参考图
接口（Seedream，`image`＝"参考图片"、无结构控制参数，三跑三套房）换到**控制通道**（万相线稿生图，
`is_sketch=true`，线稿转 90° 出图跟着转、同 seed 逐像素相同、三 seed 极差 0.004）。三判据已过，
本模块按那条通路的契约写：条件图只有一路线稿、seed 是正式参数。

**渲染只有一档**（用户裁决 2026-09-04）：没有 preview/final 参数。

**默认不摆任何家具和陈设**（同日裁决：陈设有无、位置、尺寸算设计，样式、材质算观感——设计上游
不存在，所以模型不许自己摆）：提示词固定带"无陈设"句，只渲墙面、地面、天花、门窗的材质和光。
真跑实测（《评审/失效清单-控制图通路-2026-09-04.md》C1/C2）：默认提示词下揭顶视角 6/6 自己摆家具；
"不摆陈设"句在两种视角都基本听话，残留是植物、碗碟、开关插座一类小件。

**风格是数据**（`templates/realism/*.json`）：模板本体只有风格文字与禁令；视角句、几何约束句、
无陈设句、无文字句是固定的，在本模块——它们不是风格，是这条通路的规矩。

**后端是可换的**（`RealismBackend`）：业务只认逻辑模型名 `realism-pass.default`，物理模型在网关
配置里；换后端（自部署 ControlNet 是第二形态）加一个实现、配置里点名，代码里不出现厂商名。
"""

from __future__ import annotations

import hashlib
import io
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
from PIL import Image

from imagegen_worker import fidelity_metric, fidelity_metric_v3, image_gateway
from imagegen_worker.models import RealismStyleTemplate, ViewKind

REALISM_MODEL = "realism-pass.default"
"""逻辑模型名（物理映射在 infra 的网关配置里，换模型不动代码）。"""

GATEWAY_SKETCH_BACKEND_NAME = "gateway-sketch"
"""走网关的线稿控制后端在配置里的名字（`ISHOME_REALISM_BACKEND`），也是默认值。"""

BACKEND_ENV = "ISHOME_REALISM_BACKEND"
MIN_FIDELITY_SCORE_ENV = "ISHOME_REALISM_MIN_FIDELITY_SCORE"

METRIC_VERSIONS: tuple[str, ...] = ("v2", "v3")
"""门禁跑哪几把尺子。**判只按 v2**（`fidelity_score` 对下限）；v3 的两个分数与配准只进回执，
给定阈值攒分布（`_iteration/run-2026-09-05-metric-v3/run.md` §六：整图错位算不算几何错、
阈值取多少，待拍）。"""

_GATEWAY_TIMEOUT_SECONDS = 300
"""万相线稿生图真跑 17.9–95.1 s（含排队）；沿用网关那条路的上限。"""

_IMAGE_MAGICS = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")
"""回执里的字节是不是一张图，按首部判。后端回什么格式是它的事（真跑实测万相回 PNG），
键与 Content-Type 由存储层按同一口径判（裁决 2026-09-02"标签跟着内容走"）。"""


class RealismError(Exception):
    """写实化出不来。响亮失败，不给一张"差不多的"图。"""

    def __init__(self, details: list[str]) -> None:
        super().__init__("；".join(details))
        self.details = details


# ---------------------------------------------------------------------------
# 控制稿
# ---------------------------------------------------------------------------


def build_control_sketch(line_png: bytes) -> bytes:
    """render3d 的 line/sketch（黑底白线单通道 0/255）→ 白底黑线 RGB PNG，送模型当线稿条件图。

    **只反色，不缩放、不抗锯齿、不阈值化**：每个像素严格 `255 - v`，尺寸原样。控制通道的性质是
    "线稿里画什么，出图就锁什么"——这一步任何一次重采样都会把我们的几何改掉一点。
    调研那批真跑送的正是这份反色稿（render3d `_iteration/run-2026-09-02-control-path-survey/
    万相-doodle/条件图-线稿反色.png`，逐像素等于 `255 - line.png`）。
    """
    if not line_png:
        raise RealismError(["线稿是空的：没有线稿就没有几何来源，不往下走"])
    try:
        image = Image.open(io.BytesIO(line_png))
        image.load()
    except (OSError, SyntaxError, ValueError) as e:
        raise RealismError([f"线稿解不成图（{e}）：做不出控制稿"]) from e
    gray = np.asarray(image.convert("L"), dtype=np.uint8)
    inverted: Any = (255 - gray).astype(np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(inverted, mode="L").convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

_VIEW_CLAUSES: dict[str, str] = {
    "bird": "揭顶式户型鸟瞰效果图：去掉天花板从上方斜俯视整套公寓",
    "room": "室内效果图：从房间内的机位平视这间房，取景与透视严格按线稿",
}
"""视角句。`bird` 那句是调研真跑的原话（三判据用它过的）；`room` 那句没有真跑原话可抄
（9-04 室内那批的提示词是命令行临时给的、没留档），是执行者按同一形态写的——**未经真跑，
不是裁决**，第一次室内真跑时按结果改。"""

_GEOMETRY_CLAUSE = "墙体、门窗洞口和房间划分严格按线稿，不增减墙体、不合并或拆分房间"
"""几何约束句（调研真跑原话）。几何唯一源是线稿，这句是对模型重申，不是靠它锁几何——锁几何
靠的是控制通道本身；这句管的是它在线稿没画到的地方别自作主张。"""

_NO_FURNISHING_CLAUSE = (
    "不摆放任何家具和陈设，所有房间都是空房，只渲染墙面、地面、天花和门窗的材质与光"
)
"""无陈设句（用户裁决 2026-09-04 的口径：陈设归设计，设计上游不存在就不许模型摆）。"""

_NO_TEXT_CLAUSE = "无文字、无水印、无标注"
"""不出字（调研真跑原话）。"""


def build_realism_prompt(template: RealismStyleTemplate, view_kind: ViewKind) -> str:
    """拼出发给模型的那段话。顺序固定：视角 → 几何约束 → 风格 → 无陈设 → 无文字 → 禁令。

    顺序固定是为了**同样的输入拼出同样的一段话**——提示词自己得先是确定的，才谈得上比较两次
    生成的差别来自模型还是来自我们（回执里带 `prompt_sha256` 就是为此）。
    中文写，因为这条通路的模型是中文优先、调研真跑用的就是中文。
    """
    sentences = [
        f"{_VIEW_CLAUSES[view_kind]}，{_GEOMETRY_CLAUSE}",
        template.style,
        _NO_FURNISHING_CLAUSE,
        _NO_TEXT_CLAUSE,
    ]
    if template.negatives:
        sentences.append("避免：" + "；".join(template.negatives))
    return "。".join(sentences) + "。"


def prompt_sha256_of(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 后端适配
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RealismCapabilities:
    """一个后端吃什么、保证什么。**说事实不说愿望**：拿不到的写 None，不填猜的值。"""

    condition_channels: tuple[str, ...]
    """吃哪几路条件图（`sketch` / `depth` / `mask` …）。今天只有 `sketch` 一路有通路。"""

    seed_is_effective: bool
    """同 seed 是否逐像素可复现。"""

    max_images_per_call: int

    max_output_px: tuple[int, int] | None
    """出图最大分辨率（宽, 高）；None＝契约没登记、本仓不猜。"""


@dataclass(frozen=True)
class RealismOutput:
    """后端一次出图的产物与自证数。"""

    image_bytes: bytes
    backend_name: str
    seed: int | None
    """真正送给后端的 seed；None＝没给（那一跑不可复现，回执如实写 None）。"""

    elapsed_seconds: float
    raw_meta: dict[str, Any]
    """后端回执里除图之外的部分（usage 等），如实收下不解释。"""


class RealismBackend(Protocol):
    """写实化后端。实现件：`GatewaySketchBackend`（走网关）；测试用假后端；将来的自部署形态。"""

    @property
    def name(self) -> str: ...

    def capabilities(self) -> RealismCapabilities: ...

    def generate(self, sketch_png: bytes, prompt: str, seed: int | None) -> RealismOutput:
        """一张控制稿 + 一段提示词 → 一张图。出不来抛 `RealismError`，不回空图。"""
        ...


def _looks_like_an_image(image_bytes: bytes) -> bool:
    return any(image_bytes.startswith(magic) for magic in _IMAGE_MAGICS)


class GatewaySketchBackend:
    """走 LiteLLM 网关的线稿控制后端（`POST /v1/images/generations`，`is_sketch: true`）。

    请求与错误形态照 `image_gateway`（那一层只认逻辑模型名与字节）。**因果自检**：HTTP 非 2xx、
    回执没有图、回执里的不是图，都响亮失败。**最多重试一次**，且只对连不上/超时/5xx 这类
    "再发一次可能就好"的失败重试；4xx（契约不对，如 `is_sketch` 缺失）与回执畸形再发一次也是
    同一个结果，只会再花一次钱，直接失败交给编排定。
    """

    def __init__(
        self,
        api_key: str,
        gateway_url: str = image_gateway.DEFAULT_GATEWAY_URL,
        timeout_seconds: int = _GATEWAY_TIMEOUT_SECONDS,
    ) -> None:
        if not api_key:
            raise RealismError(
                ["没有网关 key：出图要经网关，凭证放 ~/.ishome/llm-local.env（不入库）"]
            )
        self._api_key = api_key
        self._gateway_url = gateway_url
        self._timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return GATEWAY_SKETCH_BACKEND_NAME

    def capabilities(self) -> RealismCapabilities:
        # 契约里写死的：一路线稿、n=1、seed 可选（同 seed 逐像素相同是调研真跑量出来的）。
        # 最大分辨率契约没登记：真跑观测出图 1168×880（失效清单 E1），那是观测不是上限，不填。
        return RealismCapabilities(
            condition_channels=("sketch",),
            seed_is_effective=True,
            max_images_per_call=1,
            max_output_px=None,
        )

    def generate(self, sketch_png: bytes, prompt: str, seed: int | None) -> RealismOutput:
        started = time.monotonic()
        attempts_left = 2
        while True:
            attempts_left -= 1
            try:
                image_bytes, raw_meta = image_gateway.generate_from_sketch(
                    model=REALISM_MODEL,
                    prompt=prompt,
                    sketch_png=sketch_png,
                    seed=seed,
                    api_key=self._api_key,
                    gateway_url=self._gateway_url,
                    timeout_seconds=self._timeout_seconds,
                )
            except image_gateway.ImageGatewayError as e:
                if e.transient and attempts_left > 0:
                    continue
                raise RealismError(e.details) from e
            break
        if not _looks_like_an_image(image_bytes):
            raise RealismError(
                [f"回执里的字节不是一张图（首部 {image_bytes[:8]!r}）：不拿着它往下走"]
            )
        return RealismOutput(
            image_bytes=image_bytes,
            backend_name=self.name,
            seed=seed,
            elapsed_seconds=time.monotonic() - started,
            raw_meta=raw_meta,
        )


_BACKEND_FACTORIES: dict[str, Callable[[str, str], RealismBackend]] = {
    GATEWAY_SKETCH_BACKEND_NAME: lambda api_key, gateway_url: GatewaySketchBackend(
        api_key, gateway_url
    ),
}
"""配置名 → 后端。**只在这一处登记**；配置里点名，代码里不出现厂商名。"""


def backend_from_config(name: str, *, api_key: str, gateway_url: str) -> RealismBackend:
    """按配置名装后端（`ISHOME_REALISM_BACKEND`，缺省 `gateway-sketch`）。认不得的名字当场失败。"""
    factory = _BACKEND_FACTORIES.get(name)
    if factory is None:
        known = "、".join(sorted(_BACKEND_FACTORIES))
        raise RealismError([f"没有叫 `{name}` 的写实化后端：本仓认得 {known}"])
    return factory(api_key, gateway_url)


def backend_name_from_env(environ: Mapping[str, str] = os.environ) -> str:
    return environ.get(BACKEND_ENV, "").strip() or GATEWAY_SKETCH_BACKEND_NAME


# ---------------------------------------------------------------------------
# 一次写实化
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RealismVisual:
    """一次写实化的产物：图 + 送出去的控制稿与提示词（留档）+ 后端自证数。"""

    image_bytes: bytes
    control_sketch_png: bytes
    prompt: str
    prompt_sha256: str
    output: RealismOutput


def render_realism_visual(
    *,
    line_png: bytes,
    template: RealismStyleTemplate,
    view_kind: ViewKind,
    seed: int | None,
    backend: RealismBackend,
) -> RealismVisual:
    """线稿 + 风格模板 + 视角 → 一张写实图。

    两条路（CLI / activity）共用这一份，区别只在线稿字节从哪儿来（磁盘 / 私有桶）。
    """
    sketch_png = build_control_sketch(line_png)
    prompt = build_realism_prompt(template, view_kind)
    output = backend.generate(sketch_png, prompt, seed)
    if not output.image_bytes:
        raise RealismError([f"后端 `{output.backend_name}` 回了空图：不往下走"])
    return RealismVisual(
        image_bytes=output.image_bytes,
        control_sketch_png=sketch_png,
        prompt=prompt,
        prompt_sha256=prompt_sha256_of(prompt),
        output=output,
    )


# ---------------------------------------------------------------------------
# 出口门禁
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FidelityV3Measurement:
    """v3 尺子量出来的数：原位分、配准后分、配准 (sx, sy, dx, dy)。

    `score_at_origin` 只含分母改法（与 v2 直接对照）；`score_registered` 再含 ±24 px 平移配准；
    (dx, dy)＝线稿要往右 / 往下挪多少像素才与结果最对齐，(sx, sy) 不搜缩放时恒为 (1, 1)。
    """

    score_at_origin: float
    score_registered: float
    sx: float
    sy: float
    dx: int
    dy: int

    def registration(self) -> dict[str, float | int]:
        return {"sx": self.sx, "sy": self.sy, "dx": self.dx, "dy": self.dy}


def _measure_v3(
    line_png: bytes, result_png: bytes
) -> tuple[FidelityV3Measurement | None, str | None]:
    """量 v3，只记录：量不出回 (None, 原因)。

    与 v2 的响亮失败不同——v3 是候选尺子、不参与判，它任何一种失败都不许拖垮出图，
    所以这里连非度量类异常也接住，如实记成一句话进回执（`v3_error`）。
    """
    try:
        v3 = fidelity_metric_v3.score_fidelity_v3(line_png, result_png)
    except Exception as e:  # 宽捕获是有意的，见 docstring：候选尺子只记录，不许拖垮出图
        return None, f"{type(e).__name__}: {e}"
    sx, sy = v3.scale_xy
    dx, dy = v3.offset_px
    return FidelityV3Measurement(v3.score_at_origin, v3.score, sx, sy, dx, dy), None


@dataclass(frozen=True)
class RealismGateVerdict:
    """门禁的结论：分数永远有；判不判、过没过，看有没有下限。v3 的数只记录。"""

    fidelity_score: float
    min_fidelity_score: float | None
    judged: bool
    """False＝没有下限，只记录不判（`passed` 为 None）。"""

    passed: bool | None
    reason: str | None
    """不过线的原因（只在 `passed is False` 时有）。"""

    v3: FidelityV3Measurement | None
    """v3 记录项；量不出时 None、原因在 `v3_error`。不参与判。"""

    v3_error: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "fidelity_score": self.fidelity_score,
            "min_fidelity_score": self.min_fidelity_score,
            "judged": self.judged,
            "passed": self.passed,
            "reason": self.reason,
            "fidelity_v3_at_origin": self.v3.score_at_origin if self.v3 else None,
            "fidelity_v3_registered": self.v3.score_registered if self.v3 else None,
            "registration": self.v3.registration() if self.v3 else None,
            "v3_error": self.v3_error,
            "metric_versions": list(METRIC_VERSIONS),
        }


class RealismGate:
    """出口门禁，只做两件：算分、与配置里的下限比。

    算分是两把尺子：v2（`fidelity_metric`）出 `fidelity_score`，**判只按它**；v3
    （`fidelity_metric_v3`）出原位分、配准后分与配准位移，**只记录**（`METRIC_VERSIONS`）。
    **下限默认 None＝只记录不判**（《纪律·阈值有数据才定》；且尺子有两条已知限制，见
    `fidelity_metric` 模块文档——分数只能同输出形态横向比、透视视角 v2 分不开正确配对与错配）。
    给了下限才判，不过线给出原因。**不做重出循环**——重出几次、换不换 seed 是编排的事
    （《方案/挑图与保真度门禁方案》§二），门禁只回答"这一张过不过"。
    """

    def __init__(self, min_fidelity_score: float | None = None) -> None:
        if min_fidelity_score is not None and not 0.0 <= min_fidelity_score <= 1.0:
            raise RealismError(
                [f"保真度下限要在 [0, 1] 里（分数是命中比例）：给的是 {min_fidelity_score}"]
            )
        self._min_fidelity_score = min_fidelity_score

    @property
    def min_fidelity_score(self) -> float | None:
        return self._min_fidelity_score

    @staticmethod
    def from_env(environ: Mapping[str, str] = os.environ) -> RealismGate:
        """从配置读下限（`ISHOME_REALISM_MIN_FIDELITY_SCORE`）；没配＝None＝只记录不判。"""
        raw = environ.get(MIN_FIDELITY_SCORE_ENV, "").strip()
        if not raw:
            return RealismGate(None)
        try:
            return RealismGate(float(raw))
        except ValueError as e:
            raise RealismError([f"{MIN_FIDELITY_SCORE_ENV} 不是数：`{raw}`"]) from e

    def judge(self, line_png: bytes, result_png: bytes) -> RealismGateVerdict:
        """量一次分；有下限就判。v2 量不出照样响亮失败——门禁不给 0 分放行也不给 0 分拦下；
        v3 量不出只记 `v3_error`，出图不受影响。"""
        try:
            score = fidelity_metric.score_fidelity(line_png, result_png)
        except fidelity_metric.FidelityMetricError as e:
            raise RealismError([f"保真度量不出来：{e}"]) from e
        v3, v3_error = _measure_v3(line_png, result_png)
        if self._min_fidelity_score is None:
            return RealismGateVerdict(
                score, None, judged=False, passed=None, reason=None, v3=v3, v3_error=v3_error
            )
        passed = score >= self._min_fidelity_score
        reason = (
            None
            if passed
            else (
                f"保真度 {score:.4f} 低于下限 {self._min_fidelity_score:.4f}"
                "（结构边重合度，同形态横向比）"
            )
        )
        return RealismGateVerdict(
            score,
            self._min_fidelity_score,
            judged=True,
            passed=passed,
            reason=reason,
            v3=v3,
            v3_error=v3_error,
        )
