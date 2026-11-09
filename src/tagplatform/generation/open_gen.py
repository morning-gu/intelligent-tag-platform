"""模式 B：开放式标签生成（2026-11 起为 MovieTagGen 移植版）。

历史实现（单轮 prompt + 归一匹配）已被整体替换，代码不再保留：现在
`produce()` 只是把执行器上下文交给 `generation/mtg/pipeline.py` 的
`run_open_generation()`，真正的算法（多轮生成、语义合并、judge 审核、
证据校验）都在 `generation/mtg/` 包里。

对执行器的两个声明：
- `builds_prompt = True`：本模式自己拼 prompt（多轮、多模态、维度固定），
  不需要 runner 取 DB 里的 open 模板，`spec.prompt_text` 传空串；
- `prompt_version = MTG_VERSION`：任务表记录口径版本（如 `mtg-v1`），
  换算法时能区分历史结果。

空目录语义保留：开放式生成不依赖既有标签库；库非空时按 `generation/mtg/
catalog_align.py` 的口径给候选分档（同名 → origin=catalog，疑似/新词 →
generated 并只进审计与排序），实现"用库提升新标签质量"而不写库。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from tagplatform.generation.base import GenerationSpec, ProducedTag, register_producer
from tagplatform.generation.mtg.pipeline import MTG_VERSION, run_open_generation


class OpenTagProducer:
    """开放式标签生成：多轮生成 + 语义合并 + judge 过滤（实现见 mtg 包）。"""

    mode = "open"
    builds_prompt = True
    replaces_pending = True
    prompt_version = MTG_VERSION

    def produce(self, spec: GenerationSpec, session: Session, llm,
                embedder, judge_client=None) -> list[ProducedTag]:
        return run_open_generation(spec, session, llm, embedder,
                                   judge_client=judge_client)


register_producer(OpenTagProducer())
