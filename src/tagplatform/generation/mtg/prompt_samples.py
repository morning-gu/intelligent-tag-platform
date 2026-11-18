"""记录同一批开放生成中，每个模型阶段的首个成功请求与原始回答。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Callable

from tagplatform.llm.client import LLMResponse

logger = logging.getLogger(__name__)
# 同一进程可以并发运行多个批次，更新一组文件时串行写入。
_WRITE_LOCK = Lock()


class PromptSampleBatch:
    """由一次批量生成请求共享，各阶段独立认领一个样本。"""

    def __init__(self, directory: str):
        self._directory = Path(directory)
        self._captured: set[str] = set()

    def capture(self, stage: str, template: Callable[[], dict], client,
                messages: list[dict], run_id: str, response: LLMResponse) -> None:
        if not response.ok:
            return
        with _WRITE_LOCK:
            if stage in self._captured:
                return
            self._captured.add(stage)
            try:
                example = {
                    "run_id": run_id,
                    "model": getattr(client, "model", None),
                    "messages": messages,
                }
                files = {
                    f"{stage}_template.json": _serialize(template()),
                    f"{stage}_result.txt": response.content,
                    f"{stage}_example.json": _serialize(example),
                }
                self._directory.mkdir(parents=True, exist_ok=True)
                with TemporaryDirectory(dir=self._directory, prefix=".sample-") as temp:
                    staging = Path(temp)
                    for name, value in files.items():
                        (staging / name).write_text(value, encoding="utf-8", newline="")
                    for name in files:
                        (staging / name).replace(self._directory / name)
            except (OSError, TypeError, ValueError) as exc:
                logger.warning("开放生成提示词样本写入失败 stage=%s run=%s: %s",
                               stage, run_id, exc)


class SampledPromptClient:
    """沿用原客户端，只在成功返回后把当次原始消息送给批次采集器。"""

    def __init__(self, client, samples: PromptSampleBatch, stage: str,
                 template: Callable[[], dict]):
        self._client = client
        self._samples = samples
        self._stage = stage
        self._template = template

    def complete(self, messages: list[dict], run_id: str) -> LLMResponse:
        response = self._client.complete(messages, run_id)
        self._samples.capture(self._stage, self._template, self._client,
                              messages, run_id, response)
        return response


def _serialize(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
