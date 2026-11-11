"""TagProducer 协议、生成契约 dataclass 与生产器注册表（Task 3）。

生成引擎的协议层：四个 dataclass（GenerationPolicy/GenerationSpec/
Evidence/ProducedTag）定义调度与审核两侧共享的契约；TagProducer 协议
约定一种生成模式实现的最小接口；注册表为模块级 dict（照抄阶段 1
`tagplatform/adapters/base.py` 模式，单进程足够，不做插件发现）。

反失败承诺一（设计文档 §9.2）：新增第三种生成模式 = 新实现类 +
`register_producer` 注册即可，调度（Task 8 经 `get_producer(task.mode)`
分发）、审核与前端自动兼容。closed/open 两个实现在任务 5/6 的各自
模块导入时自动注册，本模块不含任何实现。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy.orm import Session

from tagplatform.schemas import ContentJSON


@dataclass
class GenerationPolicy:
    """单任务生成策略：产出上限、置信度门槛与目录范围。

    - `dir_ids`：本次生成的范围（目录 id 列表；空 = 全库）。closed 只在这些
      目录（含子目录）里召回，open 只在这些目录里对齐与产新词；
    - `allow_new_tags`：是否允许产出库外新词。False 时只出"命中库内"的行，
      适合只做补全、不想扩库的场景；
    - `max_tags_per_dir`：单目录产出上限（0 = 不限），防止一个目录刷满队列。
    """

    max_tags: int = 30
    min_confidence: float = 0.7
    dir_ids: list[str] = field(default_factory=list)
    allow_new_tags: bool = True
    max_tags_per_dir: int = 0


@dataclass
class Evidence:
    """单条标签证据：锚定到统一内容 JSON 的场景时间区间。"""

    scene_id: str
    start_ms: int
    end_ms: int
    description: str


@dataclass
class ProducedTag:
    """Producer 产出的一枚候选标签（审核前的中间态，不直接入库）。

    - `origin` 是**库内命中判定**：`"catalog"` 命中库内标签（tag_id 必填、
      candidate_* 为 None）、`"generated"` 模型新造候选（tag_id 为 None、
      candidate_* 描述新标签）。注意它与 `pre_asset_tag.origin` 列（任务的
      closed/open 产出来源）不是同一概念，落库时由任务模式决定。
    - `dir_id`：候选归属目录。catalog 取标签自身所在目录；generated 取
      可扩展目录里最相近的那个（生成时按目录范围与可否扩展决定）。
    - `target_type/target_id`：挂载粒度与挂载点，由目录的 applicable_scope
      决定——content → asset_id、scene → scene_id、character → character_id。
    - `alias_name`：命中库内时模型给出的原词（同名时为空）。它将来写进
      `tag_define.aliases`，本期先随行保存，供入库预览用。
    - `level` 为历史字段（film/scene），保留兼容旧调用。
    """

    origin: str  # "catalog" | "generated"
    tag_id: str | None
    tag_name: str
    candidate_type: str | None
    candidate_description: str | None
    relevance_score: float | None
    confidence: float
    evidence: list[Evidence]
    # 标签层级：film（影片级）/ scene（场景级）。默认 film 让闭环与目录
    # 产出无需显式传参；开放生成按 MTG 合并结果的 scope 落值。
    level: str = "film"
    # ---- v2 增量：目录归属与挂载点 ----
    dir_id: str | None = None
    target_type: str = "content"
    target_id: str | None = None
    alias_name: str | None = None


@dataclass
class GenerationSpec:
    """单次生成的完整输入：任务标识 + 内容 + 策略 + 渲染后 prompt。

    `library` 是本轮的标签库（JSON 模拟）；None 表示按无库运行（对齐与
    新词归属都退化为"没有可参照的库"）。
    """

    task_id: str
    asset_id: str
    content: ContentJSON
    policy: GenerationPolicy
    prompt_text: str
    run_id: str
    library: object | None = None


class TagProducer(Protocol):
    """标签生成器协议。

    约定：
    - `mode` 唯一标识生成模式（closed/open 在任务 5/6 注册），同一
      mode 不可重复注册；
    - `produce` 返回 ProducedTag 列表，允许返回空列表（生成不出来
      不算失败）。
    """

    mode: str

    def produce(
        self,
        spec: GenerationSpec,
        session: Session,
        llm,
        embedder,
    ) -> list[ProducedTag]:
        """按 spec 生成候选标签列表。"""
        ...


# 模块级注册表：mode -> 生成器实例（同 adapters 模式，单进程足够）
_REGISTRY: dict[str, TagProducer] = {}


def register_producer(producer: TagProducer) -> None:
    """注册生成器；mode 重复抛 ValueError。"""
    if producer.mode in _REGISTRY:
        raise ValueError(
            f"生成器 mode 已注册: {producer.mode}（可用: {list_producers()}）"
        )
    _REGISTRY[producer.mode] = producer


def get_producer(mode: str) -> TagProducer:
    """按 mode 取生成器；未注册抛 KeyError，信息列出可用 mode。"""
    try:
        return _REGISTRY[mode]
    except KeyError:
        raise KeyError(
            f"未注册的生成器 mode: {mode!r}（可用: {list_producers()}）"
        ) from None


def list_producers() -> list[str]:
    """列出全部已注册 mode（排序返回，保证确定性）。"""
    return sorted(_REGISTRY)
