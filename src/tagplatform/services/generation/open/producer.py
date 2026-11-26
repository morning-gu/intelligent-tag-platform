"""模式 B：开放式标签生成（2026-11 起为 MovieTagGen 移植版）。

历史实现（单轮 prompt + 归一匹配）已被整体替换，代码不再保留：现在
`produce()` 只是把执行器上下文交给 `open/mtg/pipeline.py` 的
`run_open_generation()`，真正的算法（多轮生成、语义合并、judge 审核、
证据校验）都在 `open/mtg/` 包里。

对执行器的两个声明：
- `builds_prompt = True`：本模式自己拼 prompt（多轮、多模态、维度固定），
  不需要 runner 取 DB 里的 open 模板，`spec.prompt_text` 传空串；
- `prompt_version = MTG_VERSION`：任务表记录口径版本（如 `mtg-v1`），
  换算法时能区分历史结果。

空目录语义保留：开放式生成不依赖既有标签库；库非空时按 `open/mtg/
catalog_align.py` 的口径给候选分档（同名 → origin=catalog，疑似/新词 →
generated 并只进审计与排序），实现"用库提升新标签质量"而不写库。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from tagplatform.services.generation.base import GenerationSpec, ProducedTag, register_producer
from tagplatform.services.generation.open.mtg.pipeline import MTG_VERSION, run_open_generation
from tagplatform.settings import Settings


class OpenTagProducer:
    """开放标签生成，委托逐目录两阶段管线。"""

    mode = "open"
    builds_prompt = True
    prompt_version = MTG_VERSION

    def produce(self, spec: GenerationSpec, session: Session, llm,
                embedder, judge_client=None,
                settings: Settings | None = None,
                trace=None) -> list[ProducedTag]:
        """执行开放生成管线；trace 接受并忽略，开放侧语义未实现（漏斗
        追踪为 closed 侧采集，此处仅为协议签名兼容）。"""
        # settings：run 级副本（开放评测用），None 时由流水线回落 get_settings()。
        return run_open_generation(spec, session, llm, embedder,
                                   judge_client=judge_client, settings=settings)


register_producer(OpenTagProducer())
