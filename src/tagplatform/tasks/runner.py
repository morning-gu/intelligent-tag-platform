"""生成任务执行器：任务状态机、prompt 组装、线程池执行与孤儿清理（Task 8）。

把前序部件串成完整执行链（计划钉死的八步流程）：
状态机（queued → running → succeeded|failed，running 置位先落库防长任务
重复提交）→ 内容解析 → producer 分发（get_producer，closed/open 注册于
tagplatform.generation 导入）→ prompt 组装渲染（content 文本两模式共用，
candidates 文本仅 closed、行序 = 召回结果行序）→ policy 解析 → produce →
persist_tags（不 commit）→ 模式 A 回填媒体上下文摘要向量 → 终态一次性
commit（生成结果与任务终态同事务落库——Review Focus 1 原子性）。

事务语义：
- 失败路径统一 `rollback → failed → commit`：回滚丢弃 persist/回填的未提交
  写入，保证零半截 asset_tag；任务置 failed 后返回、不 re-raise——调用方
  （API 层/线程池）经状态与 error 字段感知失败；
- 捕获范围取 Exception 而非仅 brief 列举的 ValueError/RuntimeError/
  LLMParseError：这三类覆盖 produce 的预期失败，其余未预期异常（坏 mode、
  坏 content_json、缺激活模板等配置错误）若外抛会把任务永久滞留 running，
  任务级事务必须保证任何失败都落终态（进程崩溃除外，由孤儿重置兜底）。

TaskExecutor 为线程池封装：线程内独立 session（SQLite 文件库跨线程可见，
:memory: 不可），异常吞并记日志；workers=0 同步执行（T9 钉死的测试模式）。
reset_orphan_running 供 create_app 启动时清理进程重启留下的 running 孤儿。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from tagplatform.db.models import MediaAsset, TagGenerationTask
from tagplatform.generation import GenerationPolicy, GenerationSpec, get_producer
from tagplatform.generation.recall import (
    RecallCandidate,
    embed_content_texts,
    recall_candidates,
)
from tagplatform.prompts import get_active_prompt, render_prompt
from tagplatform.results import persist_tags
from tagplatform.content_io import load_content
from tagplatform.ingest import record_alias
from tagplatform.library import try_load_library
from tagplatform.schemas import ContentJSON, parse_content
from tagplatform.settings import get_settings

logger = logging.getLogger(__name__)


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


def _render_candidates_text(candidates: list[RecallCandidate]) -> str:
    """召回候选 → prompt 的 candidates 文本（仅模式 A，行序 = 召回结果行序）。

    每候选一行 `- {tag_id} | {tag_name} | {tag_type} | {description}`；
    首行 "## 候选标签" 是模式 A 的标志节——MockLLM 按它识别模式 A 分支并
    逐行解析候选（llm/mock.py _CLOSED_MARKER，与任务 5 测试的组装约定一致），
    真模型下同样是有效的小节标题。
    """
    lines = [
        f"- {c.tag_id} | {c.tag_name} | {c.tag_type} | {c.description}"
        for c in candidates
    ]
    return "## 候选标签\n" + "\n".join(lines)


def run_generation_task(task_id: str, session: Session, llm,
                        embedder) -> TagGenerationTask:
    """执行单个生成任务：状态机 + prompt 组装 + 生成 + 入库（内含完整事务）。

    八步流程（计划钉死）：
    1. 取 task；非 queued → ValueError("任务状态非 queued")；置 running、commit；
    2. 资产或其 content_json 缺失 → failed + error、commit、返回 task；
    3. load_content(asset.content_json)；资产 id 即 task.asset_id（任务直绑资产）；
    4. get_producer(task.mode)；closed 额外召回候选组装 candidates 文本，
       render_prompt 渲染激活模板（open 模板无 candidates 占位符，只传 content）；
    5. policy_json 空 → 默认 GenerationPolicy，否则按 JSON 构造；
    6. producer.produce(spec, session, llm, embedder)——任一异常：回滚半截
       写入后置 failed（error=str(e)、finished_at）、commit、返回 task
       （不 re-raise，调用方看状态）；
    7. persist_tags 写 pre_asset_tag（不 commit，随终态同事务落库）；
    8. task.model_version = llm.model、prompt_version = 激活模板版本
       （写入字符串）、succeeded、finished_at、一次性 commit、返回 task。
    """
    task = session.get(TagGenerationTask, task_id)
    if task is None:
        raise ValueError(f"任务不存在: {task_id}")
    if task.status != "queued":
        raise ValueError("任务状态非 queued")
    # 1. running 置位先落库：长任务期间其余会话可见，防重复提交
    task.status = "running"
    session.commit()

    # 2. 资产/内容缺失属数据错误：显式失败，不留 running 僵尸
    asset = session.get(MediaAsset, task.asset_id)
    if asset is None or not (asset.content_json or "").strip():
        task.status = "failed"
        task.error = (
            f"资产不存在: {task.asset_id}" if asset is None
            else f"资产无识别内容: {task.asset_id}")
        task.finished_at = datetime.now(timezone.utc)
        session.commit()
        return task

    try:
        # 3. 内容解析与资产归属（任务即资产，content 内 id 仅作校验兜底）
        content = load_content(asset.content_json)
        asset_id = task.asset_id

        # 4. 标签库与 producer 分发（库在任务开始时加载一次，JSON 读取有缓存）
        library = try_load_library()
        # 5. 策略解析（空 policy_json → 默认）
        raw_policy = (task.policy_json or "").strip()
        policy = (GenerationPolicy(**json.loads(raw_policy))
                  if raw_policy else GenerationPolicy())
        # 4b. producer 分发与 prompt 组装渲染
        #    builds_prompt=True 的生产器自己拼 prompt（如 open 的多轮多模态
        #    流水线），不取 DB 模板、不渲染 content 文本；其余模式走模板。
        producer = get_producer(task.mode)
        template = None
        prompt_text = ""
        if not getattr(producer, "builds_prompt", False):
            template = get_active_prompt(session, task.mode)
            fields: dict[str, str] = {"content": _render_content_text(content)}
            if task.mode == "closed":
                if library is None:
                    raise ValueError("标签库未就绪，无法执行封闭式生成")
                candidates = recall_candidates(
                    library,
                    embedder,
                    embed_content_texts(content),
                    dir_ids=policy.dir_ids,
                    top_k=get_settings().recall_top_k_per_type,
                )
                fields["candidates"] = _render_candidates_text(candidates)
            prompt_text = render_prompt(template, **fields)
        # 6. 生成（run_id 含随机段：同任务重跑换 run_id，Mock 哈希采样随之变化）
        run_id = f"{task.task_id}:{uuid4().hex[:6]}"
        spec = GenerationSpec(
            task_id=task.task_id, asset_id=asset_id, content=content,
            policy=policy, prompt_text=prompt_text, run_id=run_id,
            library=library,
        )
        tags = producer.produce(spec, session, llm, embedder)
        # 7. 结果入库（不 commit）：persist_tags 内部先清同 origin 的 pending
        #    行再逐条插入，重跑不会在审核队列里叠加历史候选
        summary = persist_tags(session, asset_id, task.task_id, tags, task.mode)
        if summary.cleared:
            logger.info("重跑替换：资产 %s 清掉 %d 条未审核候选",
                        asset_id, summary.cleared)
        # 相似并入的行把模型原词登记成别名（写 tag_define.aliases + 变更日志；
        # 失败不影响任务）。注意 record_alias 的第一个参数是 session——合 main 后
        # 别名从 JSON 库搬进了 tag_define 表，签名从 (library, ...) 改成
        # (session, ...)，这里曾漏改，导致 open 生成一走到 catalog 命中就
        # AttributeError: 'TagLibrary' object has no attribute 'get'。
        for tag in tags:
            if tag.origin == "catalog" and tag.alias_name and tag.tag_id:
                try:
                    record_alias(
                        session, tag_id=tag.tag_id, alias=tag.alias_name,
                        source={"asset_id": asset_id, "task_id": task.task_id,
                                "origin": task.mode, "auto": True})
                except (OSError, ValueError) as exc:
                    logger.warning("别名预览写入失败：%s", exc)

        # 8. 终态回填与一次性提交：生成结果 + 任务终态同事务落库
        task.model_version = llm.model
        # prompt_version 列为 TEXT：写入侧即字符串，与读回一致；
        # 自拼 prompt 的生产器写自己的口径版本（如 mtg-v1）
        task.prompt_version = (str(template.version) if template is not None
                               else getattr(producer, "prompt_version", ""))
        task.status = "succeeded"
        task.finished_at = datetime.now(timezone.utc)
        session.commit()
        return task
    except Exception as e:  # noqa: BLE001 —— 任务级事务：任何失败都落任务终态
        # 先回滚丢弃 persist/回填的未提交写入（零半截 asset_tag），再落
        # failed 终态；不 re-raise，调用方经状态与 error 感知失败
        session.rollback()
        task.status = "failed"
        task.error = str(e)
        task.finished_at = datetime.now(timezone.utc)
        session.commit()
        return task


class TaskExecutor:
    """生成任务线程池执行器：线程内独立 session 跑 run_generation_task。

    workers=0 时不建池、submit 即同步执行（Settings.generation_workers=0
    的钉死语义，测试与单步调试用）；shutdown 供应用退出时关池（T9 lifespan）。
    """

    def __init__(self, session_factory, llm, embedder, workers: int = 2):
        self._session_factory = session_factory
        self._llm = llm
        self._embedder = embedder
        self._pool = (None if workers <= 0 else
                      ThreadPoolExecutor(max_workers=workers,
                                         thread_name_prefix="generation"))

    def submit(self, task_id: str) -> None:
        """提交任务执行；workers=0 时同步执行（submit 返回即完成）。"""
        if self._pool is None:
            self._run(task_id)
            return
        self._pool.submit(self._run, task_id)

    def shutdown(self) -> None:
        """关闭线程池并等待在途任务结束（同步模式无池，空操作）。"""
        if self._pool is not None:
            self._pool.shutdown(wait=True)

    def _run(self, task_id: str) -> None:
        """工作线程体：线程内独立 session 执行单任务。

        异常吞并记日志：任务自身失败已由 run_generation_task 落库为 failed，
        能外抛的只剩基础设施级意外（会话工厂故障等）——记日志留痕，不向
        线程池外传播；滞留 running 的极端情况由重启孤儿重置兜底。
        """
        session = None
        try:
            # 会话工厂调用也在 try 内：工厂故障同样记日志（否则异常随被
            # 丢弃的 future 静默消失，无线索可查）
            session = self._session_factory()
            run_generation_task(task_id, session, self._llm, self._embedder)
        except Exception:
            logger.exception("生成任务线程执行异常 task_id=%s", task_id)
        finally:
            if session is not None:
                session.close()


def reset_orphan_running(session: Session) -> int:
    """进程重启后的孤儿清理：running 任务全部置 failed，返回复位数量。

    进程崩溃时任务来不及走失败收尾，重启后永远滞留 running——create_app
    启动时调用本函数一次性复位（error="进程重启中断"）。
    """
    orphans = (
        session.query(TagGenerationTask)
        .filter(TagGenerationTask.status == "running")
        .all()
    )
    now = datetime.now(timezone.utc)
    for task in orphans:
        task.status = "failed"
        task.error = "进程重启中断"
        task.finished_at = now
    session.commit()
    return len(orphans)
