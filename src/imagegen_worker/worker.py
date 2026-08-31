"""worker 进程装配：连接 Temporal（namespace `genpipe`），监听 `imagegen-activities`。

**组合根在此**：私有桶连接、模板库、网关凭证三样都在这里装好并当场校验——装不上就起不来，
绝不带着半套配置上线等第一张图去踩（"缺配置要在起不来的时候就知道"）。

genpipe workflow 按 activity 归属把任务派到本仓专属 task queue；重试/心跳/取消/背压沿用
Temporal activity 原生语义，不引入服务间 HTTP 调用（对齐文档 §3.1）。

**模板库是数据，从目录读**（红线：配置只放数据，逻辑归服务）。目录由
`ISHOME_IMAGEGEN_TEMPLATES_DIR` 给，不写默认路径：模板不在 wheel 里（`templates/` 在仓根、
不在包内），留一个默认值只会让部署现场以为装上了、其实装的是空库。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from pydantic import ValidationError
from temporalio.client import Client
from temporalio.worker import Worker

from imagegen_worker.activities import AtmosphereVisualGenerator, activity_registry
from imagegen_worker.atmosphere import load_template
from imagegen_worker.image_gateway import DEFAULT_GATEWAY_URL
from imagegen_worker.image_store import (
    ImageStoreError,
    OssImageStore,
    OssSettings,
    check_template_id,
)
from imagegen_worker.models import StyleTemplate

GENPIPE_NAMESPACE = "genpipe"
IMAGEGEN_TASK_QUEUE = "imagegen-activities"
"""contracts `registries/task_queues.md` 逐字一致（只增不改）。"""

TEMPLATES_DIR_ENV = "ISHOME_IMAGEGEN_TEMPLATES_DIR"
API_KEY_ENV = "LITELLM_API_KEY"
GATEWAY_URL_ENV = "LITELLM_BASE_URL"


def _load_templates() -> dict[str, StyleTemplate]:
    """装模板库：目录下每份 JSON 一个模板，**起进程时逐份校验**。

    三处当场判死，都是"装的时候不判、出图时才发现"会更贵的：模板读不动（改坏了一个字段）、
    文件名与 `templateId` 对不上（改了一个忘了另一个，而**产物的对象键里带的是 id 不是文件名**）、
    id 当不了对象键的一段。
    """
    raw_dir = os.environ.get(TEMPLATES_DIR_ENV, "").strip()
    if not raw_dir:
        raise SystemExit(
            f"没有 {TEMPLATES_DIR_ENV}：出风格图要模板库，指到本仓的 templates/ 目录"
            "（模板是数据，不编进包里）"
        )
    directory = Path(raw_dir)
    paths = sorted(directory.glob("*.json"))
    if not paths:
        raise SystemExit(f"{directory} 下一份模板都没有（*.json）——不是 templates 目录？")

    templates: dict[str, StyleTemplate] = {}
    for path in paths:
        try:
            template = load_template(path)
        except (OSError, ValueError, ValidationError) as e:
            # 起不来的原因要**一眼读得懂**：压成一行，不把 pydantic 的多行报告原样甩出来
            reason = " ".join(str(e).split())[:200]
            raise SystemExit(f"模板读不动：{path}——{reason}") from None
        if template.template_id != path.stem:
            raise SystemExit(
                f"模板文件名与 templateId 对不上：{path.name} vs `{template.template_id}`——"
                "产物的对象键里带的是 id，两处分家会让图写到一个没人去读的键上"
            )
        try:
            check_template_id(template.template_id)
        except ImageStoreError as e:
            raise SystemExit("；".join(e.details)) from None
        templates[template.template_id] = template
    return templates


def _api_key() -> str:
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        raise SystemExit(
            f"没有 {API_KEY_ENV}：出图要经 LiteLLM 网关，凭证放 ~/.ishome/llm-local.env（本机）"
            "或 /opt/ishome/env/（服务器），不入库"
        )
    return api_key


async def run_worker(temporal_address: str) -> None:
    templates = _load_templates()
    api_key = _api_key()
    try:
        store = OssImageStore(OssSettings.from_env())
    except ImageStoreError as e:
        # 缺配置是**运维要看的一句话**，不是给开发看的调用栈：起不来的原因要一眼读得懂。
        raise SystemExit("；".join(e.details)) from None
    generator = AtmosphereVisualGenerator(
        store,
        templates,
        api_key,
        os.environ.get(GATEWAY_URL_ENV, "").strip() or DEFAULT_GATEWAY_URL,
    )
    client = await Client.connect(temporal_address, namespace=GENPIPE_NAMESPACE)
    worker = Worker(
        client,
        task_queue=IMAGEGEN_TASK_QUEUE,
        activities=list(activity_registry(generator).values()),
    )
    await worker.run()


def main() -> None:
    asyncio.run(run_worker(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")))


if __name__ == "__main__":
    main()
