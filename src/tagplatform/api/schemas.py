"""API 层共享的响应字段类型。

时间戳口径（2026-11-19 起）：DB 统一存 naive UTC（写入点
`datetime.now(timezone.utc)` 经 SQLite DateTime 列亲和剥掉 tzinfo），
API 序列化时把 naive 一律视为 UTC 补回 tzinfo，JSON 输出带 `Z` 后缀
（如 `2026-11-18T10:31:32Z`）——前端解析后即可正确转本地时区展示，
修复"UTC 裸时间戳被当成本地时间读"的 8 小时误读（2026-11-18 排查
gen_4845957f9239 的根因链：任务实际 4 分钟成功，纯展示层误读）。
"""

from datetime import datetime, timezone
from typing import Annotated

from pydantic import WrapSerializer


def _serialize_utc(value: datetime, handler):
    """naive 值按 UTC 补 tzinfo 后交给 Pydantic 缺省序列化。

    历史行与现行写入点均为 UTC，故在序列化侧统一补 UTC（读侧不转换）；
    已带 tzinfo 的值原样通过。None 由 Pydantic 在进入序列化器前短路，
    不会调到这里。返回值不标类型：JSON 模式出 str、python 模式出
    datetime，标任一端都会触发 Pydantic 序列化警告。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return handler(value)


UtcDatetime = Annotated[datetime, WrapSerializer(_serialize_utc)]
"""响应模型 datetime 字段注解：naive 视为 UTC，输出 ISO-8601 带 Z。"""
