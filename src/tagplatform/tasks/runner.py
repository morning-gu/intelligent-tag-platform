"""生成任务管理器（横切执行器）：线程池调度、提交与孤儿重置。

四层重构 Task 6 定稿：本模块是**纯任务管理器**，只管任务生命周期——
线程池封装（TaskExecutor：submit_batch/submit/shutdown，workers=0 同步
模式）、进程重启孤儿清理（reset_orphan_running）。单任务的完整执行
序列（状态机 + 装配 → produce → persist，八步流程与事务语义）在
`tagplatform.services.generation.execute.run_generation_task`，本模块
线程体只调它——装配+produce+persist 业务序列住服务层（spec「tasks
纯任务管理器」定稿；评测共享装配与计算、切开持久化的复用协议见
execute.py docstring）。

TaskExecutor 为线程池封装：线程内独立 session（SQLite 文件库跨线程可见，
:memory: 不可），异常吞并记日志；workers=0 同步执行（T9 钉死的测试模式）。
reset_orphan_running 供 create_app 启动时清理进程重启留下的 running 孤儿。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from tagplatform.capabilities.storage.models import TagGenerationTask
from tagplatform.services.generation.execute import run_generation_task
from tagplatform.services.generation.open.mtg.prompt_samples import PromptSampleBatch
from tagplatform.tasks.tag_quality import QualityExecutor

logger = logging.getLogger(__name__)


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
