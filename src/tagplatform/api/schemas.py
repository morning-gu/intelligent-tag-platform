"""API 层共享的响应字段类型。

时间戳口径（2026-11-19 起）：DB 统一存 naive UTC（写入点
`datetime.now(timezone.utc)` 经 SQLite DateTime 列亲和剥掉 tzinfo），
API 序列化时把 naive 一律视为 UTC 补回 tzinfo，JSON 输出带 `Z` 后缀
（如 `2026-11-18T10:31:32Z`）——前端解析后即可正确转本地时区展示，
修复"UTC 裸时间戳被当成本地时间读"的 8 小时误读（2026-11-18 排查
gen_4845957f9239 的根因链：任务实际 4 分钟成功，纯展示层误读）。

四层重构 Task 11：响应模型随编排层下沉（orchestration/_shared 等），
而编排层不得反向 import api——`UtcDatetime` 的定义随之迁往
`tagplatform.orchestration._shared`，本模块 re-export 保持接入层
（routers）引用路径不变。
"""

from tagplatform.orchestration._shared import UtcDatetime

__all__ = ["UtcDatetime"]
