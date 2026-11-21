"""任务执行器包出口：纯任务管理器（提交/调度/线程池/孤儿重置）。

四层重构 Task 6 定稿：本包只管生成任务生命周期——TaskExecutor 线程池
封装与 reset_orphan_running 在 `runner` 子模块；单任务完整执行序列
（状态机+装配+produce+persist）在
`tagplatform.services.generation.execute.run_generation_task`，执行器只
调用它。此处重导出任务管理公共名字供 API 层与测试统一从
`tagplatform.tasks` 引用。

分层红线：本包不得 import fastapi（设计文档 §9.4）。
"""

from tagplatform.tasks.runner import (
    TaskExecutor,
    reset_orphan_running,
)

__all__ = [
    "TaskExecutor",
    "reset_orphan_running",
]
