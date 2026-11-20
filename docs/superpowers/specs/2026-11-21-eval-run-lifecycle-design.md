# 闭环评测 run 生命周期完善（结束时间 / 删除 / 池饿死修复）设计

日期：2026-11-21
状态：待批准
来源：用户三项需求 + 部署服务器事故排查（run-20261121-081903-b065 全量 131 部、并发 16
把 QueuePool（5+10=15）占死，平台全部 API 30s 超时，页面影片清单拉取失败被感知为
「搜索无法使用」；该 run 自身头 2 部亦以 `TimeoutError: QueuePool limit ...` 判死）。

## 意图

让评测 run 的历史可运营：知道每次跑多久、跑完的 run 可清理；同时根治「评测并发
把平台 API 拖死」的池饿死事故，让任何并发下页面照常可用。

## 目标

1. **结束时间**：历史表新增「结束时间」列——后端终态本就写 `finished_at` 进
   meta.json（closed_eval.py `_write_artifacts`），本设计只做透出：history() 摘要行
   → RunSummaryOut/RunStateOut → 前端列。运行中/老 run 无此字段 → null →「暂无」
   （None≠0 纪律，与现有列同口径）。
2. **删除已完成 run**：DELETE `/api/v1/eval/closed/runs/{run_id}`（admin），仅允许
   终态 run（succeeded/cancelled/aborted/interrupted/failed）；运行中 → 409 带原因；
   不存在 → 404。删除动作 = 移除磁盘 run 目录 + 清进程内注册表/取消事件表。
   DB 零写入红线不变（只动 eval_output_root 自己的产物目录）。前端历史行加「操作」
   列删除按钮（终态行才可点）+ Popconfirm 二次确认；删除后刷新列表，若报告视图正
   展示该 run 则一并收起。
3. **池饿死根治（事故修复）**：`_run_movie` 在 `assemble_spec` 返回后立即归还 DB
   会话，`produce()`（含全部 LLM 调用，分钟级）全程零 DB 连接。依据：closed
   producer 的 `produce(spec, session, llm, embedder)` **不使用 session 参数**（全部
   读 `spec.library`，assemble 阶段已装好）；`assemble_spec` 里 session 只用于
   `get_active_prompt` 模板读取（毫秒级）。eval 路径向 producer 传 `None`（契约
   Session 形参保留不动，正式执行器照常传真会话；若未来 closed producer 改用
   session，eval 会以 AttributeError 失败显式暴露，不静默）。
4. **选片搜索按 tt+标题双匹配**（顺带项，用户已选）：自定义影片 Select 的
   `filterOption` 同时匹配 `value`（tt 编号）与 `label`（标题）——现在搜
   `tt0068646` 无结果。
5. **清 print 调试残留**（顺带项，用户已选）：closed.py:125/131 两个 print 把
   完整 prompt+response 打进 server.log，全量 131 部 run 会写 GB 级日志（记忆
   待办项「print 调试残留清理」）。直接删除，不留日志级替代（debug 级 logger 已有
   组级解析计数）。

## 非目标

- 不做耗时列/平均耗时统计（用户只要结束时间；需要时后续再加）。
- 不做批量删除/按时间清理策略。
- 不加评测并发上限护栏（用户已选纯还连接方案：produce 零 DB 连接后，并发只压
  LLM 网关，不再碰池）。
- 不改 TagProducer 协议签名、不动正式生成执行器的会话语义。
- 不修 tt0117500 上下文超长（另行跟进）。

## 约束

- 评测对业务表只读红线（spec §5/§9）不变；删除只允许动 `eval_output_root` 下
  自己的 run 目录，run_id 仍经 `_run_dir` 越界守卫解析。
- 前端 null 口径统一「暂无」；删除按钮对运行中行置灰（后端 409 是最终裁决）。
- 后端删除幂等语义：重复删除已不存在的 run → 404（与 cancel 的 {ok:false} 幂等
  语义不同：删除的资源消失语义与 GET 404 同口径）。

## 方案取舍（Approaches）

**池饿死修复**：
- 方案 A（采纳）：assemble 后还连接，produce 传 None。根治——任何并发下评测
  worker 占用的 DB 连接时间从「分钟级/片」降到「毫秒级/片」。
- 方案 B（否决）：限并发上限 ≤8。治标：16 改 8 仍占 8 个连接数分钟，且限制全量
  跑速度；饿死的根因（会话跨 LLM 调用持有）仍在。
- 方案 C（否决）：评测单独引擎/池。引入第二个 SQLite 连接族，WAL 下写侧（正式
  任务）与评测读侧跨池并发复杂化，收益不抵。

**搜索双匹配**：自定义 `filterOption`（对 value 与 label 做 indexOf，均小写化），
不改选项 label 展示（仍是标题，选中标签干净）。

## 涉及面

- `src/tagplatform/closed_eval.py`：RunState/history 透出 finished_at；delete()
  服务方法；_run_movie 会话释放。
- `src/tagplatform/api/routers/eval.py`：模型 +finished_at；DELETE 端点（错误映射
  409/404）；admin 端点数 10 → 11。
- `src/tagplatform/generation/closed.py`：删 2 个 print。
- `frontend/src/api/eval.ts`：类型 +finished_at；deleteRun()。
- `frontend/src/pages/ClosedEval.tsx`：结束时间列、操作列删除、filterOption。
- 测试：test_closed_eval.py（finished_at、delete 服务、会话释放红线测试）、
  test_api_eval.py（DELETE 契约）、ClosedEval.test.tsx（列渲染、删除流、tt 搜索）。

## 留给实现

- 会话释放的验证测试形态：factory 包装计数「open 会话数」，LLM 桩在 complete()
  内断言为 0（当前代码红：会话被持有）。
- interrupted run（进程死亡态）的 delete：注册表无此 run、磁盘 meta status=
  running——按历史扫描同口径推断为可删（今天服务器上的僵尸 run 正是此形态，
  部署后即用它验证）。
