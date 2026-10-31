"""任务执行器包出口：生成任务状态机、线程池执行器与孤儿清理（Task 8）。

分层红线：本包不得 import fastapi（设计文档 §9.4）。实现集中在 `runner`
子模块（计划文件结构只拆一层的执行器模块），此处重导出公共名字供 API 层
（Task 9）与测试统一从 `tagplatform.tasks` 引用。
"""

from tagplatform.tasks.runner import (
    TaskExecutor,
    reset_orphan_running,
    run_generation_task,
)

__all__ = [
    "TaskExecutor",
    "reset_orphan_running",
    "run_generation_task",
]
