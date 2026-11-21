"""生成任务执行器：任务状态机、装配调度、线程池执行与孤儿清理（Task 8）。

把前序部件串成完整执行链（计划钉死的八步流程）：
状态机（queued → running → succeeded|failed，running 置位先落库防长任务
重复提交）→ 内容解析 → 装配（load_assembly_context + build_spec，四层
重构 Task 5 起两级化于 services/generation/assemble.py，每任务活库装配；
closed 的候选标签由 producer 在 produce 内部按目录分组注入）→ policy
解析随装配完成 → produce → persist_tags（不 commit）→ 模式 A 回填媒体
上下文摘要向量 → 终态一次性 commit（生成结果与任务终态同事务落库
——Review Focus 1 原子性）。

事务语义：
- 失败路径统一 `rollback → failed → commit`：回滚丢弃 persist/回填的未提交
  写入，保证零半截 asset_tag；任务置 failed 后返回、不 re-raise——调用方
  （API 层/线程池）经状态与 error 字段感知失败；
- 捕获范围取 Exception 而非仅 brief 列举的 ValueError/RuntimeError/
  LLMParseError：这三类覆盖 produce 的预期失败，其余未预期异常（坏 mode、
  坏 content_json 等配置错误）若外抛会把任务永久滞留 running，
  任务级事务必须保证任何失败都落终态（进程崩溃除外，由孤儿重置兜底）。

TaskExecutor 为线程池封装：线程内独立 session（SQLite 文件库跨线程可见，
:memory: 不可），异常吞并记日志；workers=0 同步执行（T9 钉死的测试模式）。
reset_orphan_running 供 create_app 启动时清理进程重启留下的 running 孤儿。

spec 组装段已迁出（Task 5 装配两级化）：生成与闭环评测（closed_eval）
共用 services/generation/assemble.py 的 load_assembly_context（唯一 DB
交互点）+ build_spec（纯内存）函数对；本模块每任务执行时 load（活库
语义）后 build。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from tagplatform.capabilities.storage.models import MediaAsset, TagGenerationTask
from tagplatform.eval.tag_quality import snapshot
from tagplatform.services.generation.assemble import (
    build_spec,
    load_assembly_context,
)
from tagplatform.services.generation.open.mtg.prompt_samples import PromptSampleBatch
from tagplatform.filtering import FilterContext, run_filter_chain
from tagplatform.results import persist_tags
from tagplatform.content_io import load_content
from tagplatform.ingest import record_alias
from tagplatform.tasks.tag_quality import QualityExecutor

logger = logging.getLogger(__name__)


def run_generation_task(
    task_id: str, session: Session, llm, embedder, *,
    prompt_samples: PromptSampleBatch | None = None,
) -> TagGenerationTask:
    """执行单个生成任务：状态机 + 装配 + 生成 + 入库（内含完整事务）。

    八步流程（计划钉死）：
    1. 取 task；非 queued → ValueError("任务状态非 queued")；置 running、commit；
    2. 资产或其 content_json 缺失 → failed + error、commit、返回 task；
    3. load_content(asset.content_json)；资产 id 即 task.asset_id（任务直绑资产）；
    4. 装配两级化（Task 5，闭环评测共用同一函数对）：load_assembly_context
       （库加载/策略解析/producer 分发/模板常量——唯一 DB 交互点，活库
       语义）→ build_spec（content 文本渲染进模板，纯内存）；
    5. policy_json 空 → 默认 GenerationPolicy，否则按 JSON 构造（load 级
       完成，坏 JSON 原样外抛走任务级事务落 failed）；
    6. context.producer.produce(spec, session, llm, embedder)——任一异常：
       回滚半截写入后置 failed（error=str(e)、finished_at）、commit、返回
       task（不 re-raise，调用方看状态）；
    7. persist_tags 写 pre_asset_tag（不 commit，随终态同事务落库）；
    8. task.model_version = llm.model、prompt_version = producer 协议声明
       的口径版本（写入字符串）、succeeded、finished_at、一次性 commit、
       返回 task。
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

        # 4/5. 装配两级化（Task 5）：load（库/策略/producer/模板常量，
        #    活库语义——每任务执行时刻的库）+ build（content 文本渲染进
        #    模板）。run_id 含随机段：同任务重跑换 run_id，Mock 哈希采样
        #    随之变化
        run_id = f"{task.task_id}:{uuid4().hex[:6]}"
        context = load_assembly_context(task.mode, task.policy_json)
        spec = build_spec(context, content=content, task_id=task.task_id,
                          asset_id=asset_id, run_id=run_id)
        spec.prompt_samples = prompt_samples
        # 6. 生成（producer 随 context 携带——注册表分发只在 load 级发生）
        tags = context.producer.produce(spec, session, llm, embedder)
        # 6.5 过滤责任链（合 main 的 filtering 能力）：质量评估 → 信息围栏，
        #     通过者才进审核队列。被丢弃的标签不落 pre 表、不以 rejected
        #     落库（rejected = 人工终审语义，重跑跳过）。两环节当前均占位
        #     透传；环节抛错走下方任务级事务：回滚 + failed，零半截写入。
        candidate_count = len(tags)
        tags, reports = run_filter_chain(
            tags,
            FilterContext(task_id=task.task_id, asset_id=asset_id,
                          mode=task.mode, content=content),
            session,
        )
        if reports:
            logger.info(
                "任务 %s 过滤汇总：%d 条候选 → %d 条入库；各环节 %s",
                task.task_id, candidate_count, len(tags),
                "、".join(f"{report.name}(保留 {report.kept}/丢弃 "
                          f"{report.dropped})" for report in reports))
        # 7. 结果入库（不 commit）：persist_tags 内部先清同 origin 的 pending
        #    行再逐条插入，重跑不会在审核队列里叠加历史候选
        summary = persist_tags(session, asset_id, task.task_id, tags, task.mode)
        # persist 口径留痕：产出 N 条 ≠ 入库 N 条——命中资产既有行（任何
        # origin、任何状态）会被静默跳过且不留痕，"选了 M 个只入库 k 个"
        # 的排查两次都扑在这个观测盲点上，故每次必打、零跳过也打
        logger.info(
            "任务 %s 入库汇总：产出 %d 条 → 新插 %d 条（自动通过 %d、"
            "挂载 %d）、撞既有行跳过 %d 条",
            task.task_id, len(tags), summary.inserted,
            summary.auto_approved, summary.mounted, summary.skipped_finalized)
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
        #    （标签库随 spec 携带，此处取 spec.library）
        snapshot(session, task, spec, summary, asset.content_json, spec.library)
        task.model_version = llm.model
        # prompt_version 列为 TEXT：写入侧即字符串，与读回一致。口径版本
        # 由 producer 协议成员声明（Task 4：closed "1"、open MTG_VERSION，
        # 注册时校验保证成员必在；Task 5 起随装配 context 携带）
        task.prompt_version = context.producer.prompt_version
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

    def __init__(self, session_factory, llm, embedder, workers: int = 2, *,
                 prompt_sample_dir: str = "", quality_executor: QualityExecutor | None = None):
        self._session_factory = session_factory
        self._llm = llm
        self._embedder = embedder
        self._prompt_sample_dir = prompt_sample_dir
        self._quality_executor = quality_executor
        self._pool = (None if workers <= 0 else
                      ThreadPoolExecutor(max_workers=workers,
                                         thread_name_prefix="generation"))

    def submit_batch(self, task_ids: list[str], *, mode: str) -> None:
        """同一批影片共享生成和质量评估的提示词采集状态。"""
        samples = (PromptSampleBatch(self._prompt_sample_dir)
                   if self._prompt_sample_dir
                   and (mode == "open" or self._quality_executor is not None)
                   else None)
        for task_id in task_ids:
            self.submit(task_id, prompt_samples=samples)

    def submit(self, task_id: str, *,
               prompt_samples: PromptSampleBatch | None = None) -> None:
        """提交任务执行；workers=0 时同步执行（submit 返回即完成）。"""
        if self._pool is None:
            self._run(task_id, prompt_samples)
            return
        self._pool.submit(self._run, task_id, prompt_samples)

    def shutdown(self) -> None:
        """关闭线程池并等待在途任务结束（同步模式无池，空操作）。"""
        if self._pool is not None:
            self._pool.shutdown(wait=True)

    def _run(self, task_id: str, prompt_samples: PromptSampleBatch | None) -> None:
        """工作线程体：线程内独立 session 执行单任务。

        异常吞并记日志：任务自身失败已由 run_generation_task 落库为 failed，
        能外抛的只剩基础设施级意外（会话工厂故障等）——记日志留痕，不向
        线程池外传播；滞留 running 的极端情况由重启孤儿重置兜底。
        """
        try:
            with self._session_factory() as session:
                task = run_generation_task(task_id, session, self._llm, self._embedder,
                                           prompt_samples=prompt_samples)
                if task.status != "succeeded" or self._quality_executor is None:
                    return
            self._quality_executor.submit(task_id, prompt_samples=prompt_samples)
        except Exception:
            logger.exception("生成任务线程执行异常 task_id=%s", task_id)


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
