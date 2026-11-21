"""生成任务执行序列（四层重构 Task 6 自 tasks/runner 迁入）。

完整执行链（计划钉死的八步流程）：状态机（queued → running →
succeeded|failed，running 置位先落库防长任务重复提交）→ 内容解析 →
装配（load_assembly_context + build_spec，两级化于同包 assemble.py，
每任务活库装配；closed 的候选标签由 producer 在 produce 内部按目录
分组注入）→ policy 解析随装配完成 → produce → 过滤责任链（合 main
的 filtering 能力，被丢弃的标签不入库）→ persist_tags（不 commit）→
模式 A 回填媒体上下文摘要向量 → 终态一次性 commit（生成结果与任务
终态同事务落库——Review Focus 1 原子性）。

调用关系：本函数由横切执行器 tasks/runner.py 的线程池（TaskExecutor）
调用——tasks 只管任务生命周期（提交/调度/线程池/孤儿重置），执行序列
住服务层（spec「tasks 纯任务管理器」定稿）。评测（closed_eval）与生成
共享装配（assemble.py 函数对）与计算（get_producer 注册表间接 →
produce），**切开持久化**（persist_tags 只在正式执行序列，评测 DB 零
写入）——spec 复用协议。

事务语义：
- 失败路径统一 `rollback → failed → commit`：回滚丢弃 persist/回填的未提交
  写入，保证零半截 asset_tag；任务置 failed 后返回、不 re-raise——调用方
  （API 层/线程池）经状态与 error 字段感知失败；
- 捕获范围取 Exception 而非仅 brief 列举的 ValueError/RuntimeError/
  LLMParseError：这三类覆盖 produce 的预期失败，其余未预期异常（坏 mode、
  坏 content_json 等配置错误）若外抛会把任务永久滞留 running，
  任务级事务必须保证任何失败都落终态（进程崩溃除外，由孤儿重置兜底）。

装配两级化（Task 5）：生成与闭环评测共用 assemble.py 的
load_assembly_context（唯一 DB 交互点）+ build_spec（纯内存）函数对；
本模块每任务执行时 load（活库语义）后 build。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from tagplatform.capabilities.storage.models import MediaAsset, TagGenerationTask
from tagplatform.services.content.content_io import load_content
from tagplatform.eval.tag_quality import snapshot
from tagplatform.services.content.ingest import record_alias
from tagplatform.services.generation.assemble import (
    build_spec,
    load_assembly_context,
)
from tagplatform.services.generation.filtering import FilterContext, run_filter_chain
from tagplatform.services.generation.open.mtg.prompt_samples import PromptSampleBatch
from tagplatform.services.generation.persist import persist_tags

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
