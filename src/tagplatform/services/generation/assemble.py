"""装配层两级化（四层重构 Task 5）：load_assembly_context + build_spec。

原 tasks/runner.assemble_spec 迁出并拆两级——生成执行（tasks/runner）与
闭环评测（closed_eval）两侧共用同一函数对，prompt/policy/库加载语义
单点维护。**零漂移契约载体**：两侧走同一装配函数对，产出口径与评测
指标才可比——任何一侧单独改装配语义都会被另一侧的断言拦下。

双语义（生成-评测解耦设计的结构核心，spec 钉死）：
- **生成 = 活库**：runner 每任务执行时调 `load_assembly_context`——装
  的是"执行时刻的库"（try_load_library 的内容指纹缓存命中即复用旧视
  图，语义等价；不给 try_load_library 加新缓存机制）；
- **评测 = 快照**：ClosedEvalService 起 run 时调**一次**，装配结果随
  _EvalJob 快照携带；干跑循环（每片/每尝试）只走 `build_spec`——run
  中途改库行/换模板不影响本 run 产出（Review Focus #4 验收口径）。

分工红线：`load_assembly_context` 是装配层**唯一** DB 交互点
（try_load_library 经参数 session 或全局引擎；模板走 get_prompt 本地
常量——Task 3 起 prompt 不读 DB，git 即版本）；`build_spec` 纯内存零
DB——评测"干跑循环 DB 零交互"由此在结构上成立，而非依赖调用纪律。

builds_prompt 规则（沿用现状）：builds_prompt=True 的生产器自拼 prompt
（如 open 的多轮多模态流水线）——context.prompt_text 为空串、build_spec
不再渲染；其余模式 context.prompt_text 持模板常量**原文**，build_spec
渲染 content 文本（_render_content_text，场景行格式是 MockLLM 证据解析
的契约，两模式共用）。

异常语义不变：坏 policy JSON、未注册 mode 等配置错误原样外抛，由调用
方的事务/校验语义落失败终态。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from tagplatform.capabilities.prompts import get_prompt, render_prompt
from tagplatform.services.tags.library import TagLibrary, try_load_library
from tagplatform.schemas import ContentJSON
from tagplatform.services.generation.base import (
    GenerationPolicy,
    GenerationSpec,
    TagProducer,
    get_producer,
)

__all__ = [
    "AssemblyContext",
    "build_spec",
    "load_assembly_context",
]


# prompt 明细注入上限：长片场景多，防止 content 文本无界增长（token 成本）
_MAX_CAST_LINES = 8
_MAX_DIALOGUE_LINES = 3
_MAX_AUDIO_EVENTS = 2
_MAX_CHARACTER_EMOTIONS = 6


def _render_cast_line(content: ContentJSON) -> str | None:
    """角色表 → 单行 "人物：..."（无角色返回 None，保证老数据渲染逐字不变）。"""
    cast = content.content.characters
    if not cast:
        return None
    names = [
        f"{ch.name}（{ch.description}）" if ch.description else ch.name
        for ch in cast[:_MAX_CAST_LINES]
    ]
    return "人物：" + "、".join(names)


def _render_scene_details(scene) -> list[str]:
    """单场景的上游扩展明细（氛围 / 地点 / 声音 / 台词）。

    缩进行、不以 "- " 开头：与场景行 `- {scene_id} [start-end] desc` 的
    解析契约（llm/mock.py _SCENE_LINE_RE）天然互斥，不会污染证据解析。
    """
    lines: list[str] = []
    if scene.emotions:
        lines.append("  氛围：" + "、".join(scene.emotions))
    emotions = [f"{ce.character}（{ce.emotion}）"
                for ce in scene.character_emotions[:_MAX_CHARACTER_EMOTIONS]]
    if not emotions:
        # 严格贴模板的内容里没有场景级人物情绪：从台词情绪回推，别让标签丢掉
        # 这条证据（沉默角色仍拿不到，这是模板的建模边界）
        seen: set[str] = set()
        for dialogue in scene.dialogues:
            tone = dialogue.emotion.text_emotion if dialogue.emotion else None
            speaker = dialogue.speaker_name or dialogue.speaker
            if tone and speaker and speaker not in seen:
                seen.add(speaker)
                emotions.append(f"{speaker}（{tone}）")
            if len(emotions) >= _MAX_CHARACTER_EMOTIONS:
                break
    if emotions:
        lines.append("  人物情绪：" + "、".join(emotions))
    if scene.location:
        lines.append(f"  地点：{scene.location}")
    if scene.background_music:
        lines.append(f"  音乐：{scene.background_music}")
    for event in scene.audio_events[:_MAX_AUDIO_EVENTS]:
        lines.append(f"  声音：{event.description}")
    for dialogue in scene.dialogues[:_MAX_DIALOGUE_LINES]:
        tone = ""
        if dialogue.emotion and dialogue.emotion.text_emotion:
            tone = f"（{dialogue.emotion.text_emotion}）"
        lines.append(f"  台词：{dialogue.speaker}{tone}：{dialogue.text}")
    return lines


def _render_content_text(content: ContentJSON) -> str:
    """内容 JSON → prompt 的 content 文本（两模式共用，格式计划钉死）。

    首行《标题》与摘要行，其后每场景一行：

    《示例电影》
    摘要：影片整体剧情摘要
    - scene_001 [120000-180000] 两名人物在仓库中发生激烈争执

    2026-11 上游对接增量：摘要后可有 "人物：" 一行；每个场景行之后可跟
    缩进的明细行（氛围/地点/声音/台词）。老数据（无角色/情绪/音频/台词）
    渲染结果与扩展前逐字一致——场景行格式是 MockLLM 证据解析的契约，
    不能改。
    """
    parts: list[str] = [f"《{content.asset.title}》",
                        f"摘要：{content.content.summary}"]
    cast = _render_cast_line(content)
    if cast:
        parts.append(cast)
    for scene in content.content.scenes:
        parts.append(
            f"- {scene.scene_id}"
            f" [{scene.time_range.start_ms}-{scene.time_range.end_ms}]"
            f" {scene.description}")
        parts.extend(_render_scene_details(scene))
    return "\n".join(parts)


@dataclass
class AssemblyContext:
    """一次装配的完整输入（库视图 + 模板原文 + 策略 + 生产器）。

    - `library`：try_load_library 的结果——库未就绪时为 None（无库运行，
      与原 assemble_spec 语义一致）；
    - `prompt_text`：builds_prompt=True 的生产器为空串；否则为模板常量
      **原文**（未渲染——content 文本随片而异，渲染留给 build_spec，
      评测路径才能一次装配、逐片渲染）；
    - `policy` / `producer`：解析后的生成策略与注册表分发的生产器。

    生命周期语义由调用方决定：runner 每任务新建（活库）；评测 run 起
    跑新建一次、随 _EvalJob 供全部片共享（快照）。
    """

    library: TagLibrary | None
    prompt_text: str
    policy: GenerationPolicy
    producer: TagProducer


def load_assembly_context(mode: str, policy_json: str = "",
                          session: Session | None = None) -> AssemblyContext:
    """装配第一级：库加载 + 策略解析 + producer 分发 + 模板常量读取。

    装配层唯一 DB 交互点：try_load_library 经参数 session（调用方自带
    会话时透传）或全局引擎（缺省 None，原 assemble_spec 同款——库在
    任务开始时按内容指纹加载，命中缓存即复用旧视图）。prompt 走
    get_prompt 本地常量（无 DB）。异常语义不变：坏 policy JSON、未注册
    mode 原样外抛。
    """
    # 标签库加载（执行时刻的库；JSON 读取有内容指纹缓存）
    library = try_load_library(session)
    # 策略解析（空 policy_json → 默认）
    raw_policy = (policy_json or "").strip()
    policy = (GenerationPolicy(**json.loads(raw_policy))
              if raw_policy else GenerationPolicy())
    # producer 分发与模板常量：builds_prompt=True 的生产器自拼 prompt
    # （如 open 的多轮多模态流水线），不取模板；其余模式取本地模板常量
    # 原文（closed 候选分组由 ClosedTagProducer.produce 注入，不经占位符）
    producer = get_producer(mode)
    prompt_text = "" if producer.builds_prompt else get_prompt(mode)
    return AssemblyContext(library=library, prompt_text=prompt_text,
                           policy=policy, producer=producer)


def build_spec(context: AssemblyContext, *, content: ContentJSON,
               task_id: str, asset_id: str, run_id: str) -> GenerationSpec:
    """装配第二级：context + 单片内容 → GenerationSpec（纯内存，零 DB）。

    prompt_text 进 spec 规则沿用现状：builds_prompt=True 的生产器传空串
    （自拼 prompt），其余用 context.prompt_text（模板原文）渲染 content
    文本。评测干跑循环只进本函数——起跑装配后变更 DB 库行不影响产出
    （快照语义，Review Focus #4 的结构保证）。
    """
    prompt_text = ""
    if not context.producer.builds_prompt:
        prompt_text = render_prompt(
            context.prompt_text, content=_render_content_text(content))
    return GenerationSpec(
        task_id=task_id, asset_id=asset_id, content=content,
        policy=context.policy, prompt_text=prompt_text, run_id=run_id,
        library=context.library,
    )
