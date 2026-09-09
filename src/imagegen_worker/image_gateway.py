"""出站边缘：图像模型网关（LiteLLM）。**只认逻辑模型名，不认物理模型**。

依赖方向：本模块**不感知上层**——不 import models/style_prompt/activities，收字符串与字节、
回字节。物理模型映射在 infra 的网关配置里，换模型不动这里一行（`atmosphere-visual.default`
现指向火山方舟 Seedream）。

**图生图容易静默退化成文生图**：`image` / `size` / `watermark` 都不是 OpenAI 标准参数，
网关一旦开 `drop_params` 就把它们丢掉，出来的图与输入无关而调用照样成功——2026-08-23 接入时
踩过，配置里对这两个模型单独关掉了 drop_params。因此本模块**必查回执里的 `input_images`**：
它不是 1，就说明输入图没送到，响亮失败而不是拿着一张凭空生成的图往下走。

**每次调用都带两个标记**（`metadata.call_point` / `metadata.run_ref`，2026-09-09）：网关那边
统一把每次调大模型记下来，而它只看得见模型名——一个模型名底下是哪一处 AI 判断、属于哪次运行，
只有发请求的这一侧知道。调用点名**必填**：漏传的地方要在测试里当场报错，不是悄悄记成一条
认不出主人的调用记录。
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)
"""出图这一步**必须在日志里看得见**（2026-09-07 定）：一张 2K 图真跑要两分钟，
"生成卡住了"与"网关慢"在业务库那段 JSON 里长得一模一样。级别与落点由进程入口
（`activity_log.configure_logging`）统一决定，本模块只管记，不碰配置——出站边缘不感知上层。"""

DEFAULT_GATEWAY_URL = "http://127.0.0.1:4000"
IMAGE_ENDPOINT = "/v1/images/generations"
DEFAULT_TIMEOUT_SECONDS = 300


class ImageGatewayError(Exception):
    """出图失败。响亮失败，不返回空图让下游拿着它跑。

    `http_status`：网关明确拒绝时的状态码；连不上/超时/回执不是 JSON 时为 None。
    `transient`：**只有**连不上、超时、5xx 这三类算"再发一次可能就好"；4xx（契约不对）与
    2xx 但回执畸形（没有图、b64 解不开）再发也是同一个结果，只会再花一次钱。调用方按它定重不重试。
    """

    def __init__(
        self, details: list[str], *, http_status: int | None = None, transient: bool = False
    ) -> None:
        super().__init__("；".join(details))
        self.details = details
        self.http_status = http_status
        self.transient = transient


def _call_markers(call_point: str, run_ref: str | None) -> dict[str, Any]:
    """这次调用记在谁名下：哪一处 AI 判断（`call_point`）、属于哪次运行（`run_ref`）。

    **放请求体顶层的 `metadata`**，不放别处——LiteLLM 把 `metadata` 当自己的字段消费，
    读源码确认过（1.100.0，`litellm/images/main.py` 的 `image_generation`）：
    `default_params = openai_params + all_litellm_params`，而 `metadata` 在 `all_litellm_params`
    里，于是它进不了 `non_default_params`，也就进不了 `optional_params`——厂商请求体是
    `{"model", "prompt", **optional_params}` 拼的，`metadata` **不会被透传给厂商**。
    出图这两个逻辑名配的 `drop_params: false` 也够不着它：drop_params 只在
    `get_optional_params_image_gen` 里生效，而 `metadata` 根本没走到那一步。
    走万相那条自定义通路同理——自定义 handler 拿到的也只有 `optional_params`。

    `run_ref` 取不到就写 None：一次运行的编号只能从现成的标识来（Temporal workflow id 一类），
    不许为了填满这个字段现编一个。
    """
    return {"call_point": call_point, "run_ref": run_ref}


def _post(url: str, api_key: str, body: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    """一次出图调用。**进出各记一条**：逻辑模型名 + 走的哪条路 + 花了多久。

    记的是**逻辑名**不是物理模型（`atmosphere-visual.default` / `realism-pass.default`）——
    换模型改的是网关配置，日志里出现物理模型名等于把映射抄了第二份，换了就对不上。
    走哪条路（图生图 / 线稿控制）也要记：两条路的失败形态不同（一条会静默退化成文生图、
    另一条网关缺参直接 4xx），只看模型名分不出是哪条。
    """
    model = str(body.get("model", "?"))
    channel = "线稿控制" if body.get("is_sketch") else "图生图"
    logger.info(
        "出图调用 model=%s 通路=%s 源图=%d字节 超时=%ds",
        model,
        channel,
        len(str(body.get("image", ""))),
        timeout_seconds,
    )
    started = time.monotonic()
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload: dict[str, Any] = json.load(response)
    except urllib.error.HTTPError as e:
        # 失败这条也要带上耗时：网关拒绝是秒级、超时是分钟级，这个数把"配置不对"与
        # "模型真的慢"分开——只看状态码分不出来。
        detail = e.read().decode()[:400]
        logger.warning(
            "出图失败 model=%s 通路=%s 耗时 %.3fs 网关拒绝 HTTP %d：%s",
            model,
            channel,
            time.monotonic() - started,
            e.code,
            detail,
        )
        raise ImageGatewayError(
            [f"网关拒绝（HTTP {e.code}）：{detail}"],
            http_status=e.code,
            transient=e.code >= 500,
        ) from e
    except (urllib.error.URLError, TimeoutError) as e:
        logger.warning(
            "出图失败 model=%s 通路=%s 耗时 %.3fs 网关连不上：%s",
            model,
            channel,
            time.monotonic() - started,
            e,
        )
        raise ImageGatewayError([f"网关连不上：{e}"], transient=True) from e
    except json.JSONDecodeError as e:
        logger.warning(
            "出图失败 model=%s 通路=%s 耗时 %.3fs 回执不是 JSON：%s",
            model,
            channel,
            time.monotonic() - started,
            e,
        )
        raise ImageGatewayError([f"网关回了不是 JSON 的东西：{e}"]) from e
    logger.info(
        "出图返回 model=%s 通路=%s 耗时 %.3fs 图数=%d input_images=%s",
        model,
        channel,
        time.monotonic() - started,
        len(payload.get("data") or []),
        payload.get("usage", {}).get("input_images"),
    )
    return payload


def generate_from_image(
    *,
    model: str,
    prompt: str,
    source_png: bytes,
    size: str,
    api_key: str,
    call_point: str,
    run_ref: str | None = None,
    gateway_url: str = DEFAULT_GATEWAY_URL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[bytes, str]:
    """图生图：一张源图 + 一段提示词 → 一张图。返回（图字节, 模型改写后的提示词）。

    `call_point` 必填、`run_ref` 可选，见 :func:`_call_markers`。
    """
    if not source_png:
        raise ImageGatewayError(["源图是空的：没有母版就没有几何来源，不往下走"])
    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "image": "data:image/png;base64," + base64.b64encode(source_png).decode(),
        "size": size,
        "watermark": False,
        "n": 1,
        "response_format": "b64_json",
        "metadata": _call_markers(call_point, run_ref),
    }
    payload = _post(gateway_url + IMAGE_ENDPOINT, api_key, body, timeout_seconds)

    input_images = payload.get("usage", {}).get("input_images")
    if input_images != 1:
        raise ImageGatewayError(
            [
                f"输入图没送到模型（回执 input_images={input_images!r}）——"
                "图生图静默退化成了文生图，出来的图与母版无关。检查网关是否对本模型丢了 image 参数"
            ]
        )

    items = payload.get("data") or []
    if not items:
        raise ImageGatewayError(["网关回了空的图片列表"])
    encoded = items[0].get("b64_json")
    if not encoded:
        raise ImageGatewayError(["回执里没有图片内容（b64_json 为空）"])
    revised = items[0].get("revised_prompt") or ""
    return base64.b64decode(encoded), str(revised)


def generate_from_sketch(
    *,
    model: str,
    prompt: str,
    sketch_png: bytes,
    seed: int | None,
    api_key: str,
    call_point: str,
    run_ref: str | None = None,
    gateway_url: str = DEFAULT_GATEWAY_URL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[bytes, dict[str, Any]]:
    """线稿生图（控制通道）：一张白底黑线的线稿 + 一段提示词 → 一张图。返回（图字节, 回执其余）。

    契约（infra 的 LiteLLM custom handler，2026-09-05）：同一个 `/v1/images/generations` 口，
    请求多带 `is_sketch: true`（**缺了网关拒绝 4xx，不静默退化**——与参考图那条路"输入图被丢、
    调用照样成功"的坑是两回事，所以这里不查 `input_images`）与可选的 `seed`。
    `image` 只一张，白底黑线 RGB PNG，几何由它定、不由模型定。

    `call_point` 必填、`run_ref` 可选，见 :func:`_call_markers`。
    """
    if not sketch_png:
        raise ImageGatewayError(["线稿是空的：没有线稿就没有几何来源，不往下走"])
    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "image": "data:image/png;base64," + base64.b64encode(sketch_png).decode(),
        "n": 1,
        "response_format": "b64_json",
        "is_sketch": True,
        "metadata": _call_markers(call_point, run_ref),
    }
    if seed is not None:
        body["seed"] = seed
    payload = _post(gateway_url + IMAGE_ENDPOINT, api_key, body, timeout_seconds)

    items = payload.get("data") or []
    if not items:
        raise ImageGatewayError(["网关回了空的图片列表"])
    encoded = items[0].get("b64_json")
    if not encoded:
        raise ImageGatewayError(["回执里没有图片内容（b64_json 为空）"])
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as e:
        raise ImageGatewayError([f"回执里的 b64_json 解不开：{e}"]) from e
    if not image_bytes:
        raise ImageGatewayError(["回执里的图片解出来是零字节"])
    raw_meta = {key: value for key, value in payload.items() if key != "data"}
    raw_meta["data_meta"] = [{k: v for k, v in item.items() if k != "b64_json"} for item in items]
    return image_bytes, raw_meta
