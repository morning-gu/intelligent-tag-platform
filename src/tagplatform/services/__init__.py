"""服务层：业务模块的住所（四层重构 Task 4 起建包）。

层定位（四层重构设计文档）：服务层承接业务规则与领域操作，向下依赖能力层
（capabilities），被编排层（orchestration）与接入层（api）调用；依赖红线
api → orchestration → services → capabilities，任何层不得反向 import。
本层不得 import fastapi（领域不碰传输协议）。

领域库/服务二分规则（设计文档"服务层模块二分"节）：
- 领域库：无状态纯函数/数据结构/契约（如 library、generation/base、
  content_io、mtg/prompts 工具）——跨域共享语言，允许被任何服务 import；
- 服务：有状态、管事务/会话/线程（如 review/service、eval/service、
  tasks/runner 调度）——彼此零直接 import；跨服务协作只经注册表
  （get_producer 式间接）或编排层装配；
- 跨域数据契约（≥3 域共享）上移契约包（schemas/ 既有实践的推广）；仅
  2 域共享的留在被依赖方底座（library 模式）。
"""
