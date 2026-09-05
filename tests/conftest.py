"""测试共用件。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from imagegen_worker import fidelity_metric_v3


@pytest.fixture(scope="module")
def memoized_fidelity_v3() -> Iterator[None]:
    """把 v3 按输入字节记住：同一对图真算一次（1280×960 上约 6 s），再量直接回上次的数。

    只给走门禁的测试模块用（`pytestmark = pytest.mark.usefixtures(...)`）：那些测试要的是回执
    形态与出图路径，不是把同一对 fixture 重复量十几次。带旋钮的调用（自证对照）不进缓存。
    尺子自己的测试（`test_fidelity_metric_v3.py`）不用它。
    """
    real = fidelity_metric_v3.score_fidelity_v3
    cache: dict[tuple[bytes, bytes], fidelity_metric_v3.FidelityV3Score] = {}

    def memoized(
        line_png: bytes, result_png: bytes, **kwargs: Any
    ) -> fidelity_metric_v3.FidelityV3Score:
        if kwargs:
            return real(line_png, result_png, **kwargs)
        key = (line_png, result_png)
        if key not in cache:
            cache[key] = real(line_png, result_png)
        return cache[key]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fidelity_metric_v3, "score_fidelity_v3", memoized)
        yield
