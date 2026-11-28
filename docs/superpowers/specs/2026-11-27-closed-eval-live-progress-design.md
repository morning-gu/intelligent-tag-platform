# 闭环评测实时进度明细设计（短文档）

日期：2026-11-27 · 状态：待批准（对话内拍板：看什么=逐片结果+指标好坏，聚合留终态；入口=独立进度明细视图；interrupted run=也路由到该视图）
前置：2026-11-26-closed-eval-funnel-design.md（funnel trace 已合 main=4028498 并部署）＋ 开放评测 live.json 机制（`open_service.py:_publish_live` / `orchestration/eval.py:776` 报告端点回落序）

## 意图

闭环评测一轮全集按并发 4 要跑半小时到一小时以上，期间只有进行中卡片的 done/total/failed 数字，逐片明细/漏斗/trace 全要等终态。参照开放评测的 live.json 机制补「跑中可看逐片结果与指标」，与刚上线的漏斗功能互补（每片 trace 边跑边亮）。

## 目标

- 运行中 run 可打开**进度明细视图**：运行横幅（状态/已完成/失败/自动刷新/取消）+ 复用整块 `ClosedEvalMovieTable`（θ 门、整体漏斗块、逐片漏斗 trace、失败置顶、llm-raw 下载）；
- 2s 轮询，行边跑边长；run 转终态自动切报告视图；
- interrupted run（报告永不落盘）也路由到该视图——live.json 兜底照常出部分明细，进程死亡后跑到哪一目了然。

## 非目标

- run 级聚合指标（nDCG@10 等）不做运行中版本（部分片均值误导 + 报告 banner 终态语义）——`get_run_report` 不动，仍 report.json 专用；
- 报告三件套写序/落盘格式不动（meta → raw_results → report 原样，live.json 是新增旁路产物）；
- 生产路径（producer/execute.py）零改动（本轮全部在评测服务写侧 + 读侧 + 前端）；
- 老中断 run / 上线前 run（无 live.json）不追溯——按「暂无」404 处理（None≠0 纪律）。

## 方案（已选）

照抄开放评测 live.json 模式（唯一合理路线——闭环逐片指标本就读侧现算，喂同一读路径即全部就位）。被否备选：

- **直读 progress.jsonl**：行无 gt/title，数据集根 override 中途可变 → 另养一套读语义，与终态口径分裂；
- **纯内存端点**：进程死亡即失（live.json 落盘反而白赚「中断 run 部分明细可查」），违背评测产物全磁盘纪律。

## 设计要点

- **写侧**（`service.py`）：`_write_artifacts` 的行构造抽共用 helper（tt/title/ok/error/produced/gt/elapsed_ms/trace?——与 raw_results.json 严格同形）；起跑随 meta 骨架一并写**空 live.json**（rows=[]，读侧从 t=0 起统一口径）；收敛循环每完成一片（`_record_result` 之后；停止后到达的结果照发）调 `_publish_live(job, results)`——tmp+rename 原子写 live.json，worker 单线程处理完成结果无锁竞争，发布失败只记日志（live 是旁路产物，不拖垮评测——open 同款容错）。DB 零写入红线不变。
- **读侧**（`orchestration/eval.py:_movies_read`）：回落序 raw_results.json → live.json → 404「无单片明细」。live.json 截断/损坏按缺失容错。detail / llm-raw 端点经 `_movies_read` 自动随动（llm-raw 的 tt-in-rows 守卫对在途片自然 404——llm_raw 文件本来跑时逐片落）。
- **终态清理**：`_write_artifacts` 写完 raw_results.json 后删 live.json（unlink 容错）——raw 优先级已盖过它，删除保持 run 目录语义干净；中断残留的 live.json 即该 run 的部分明细。
- **前端**（`ClosedEval.tsx`）：历史行点击路由扩展——running / interrupted → 进度明细视图，其余 → 报告视图（现状）；活跃卡加「查看进度明细」按钮。进度明细视图 = 运行横幅（run_id/状态/已完成/失败 + 「每 2 秒自动刷新」+ 取消复用 handleCancel）+ `<ClosedEvalMovieTable refreshTick={n}/>`；页面既有 2s 轮询周期里 bump tick → 表重拉。转终态：自动尝试切报告视图（getReport 的 meta→raw→report 写序窗口沿用「稍后重试」提示）。interrupted 打开的视图不轮询，横幅标「已中断」。
- **组件小改**（`ClosedEvalMovieTable.tsx`）：新增 `refreshTick?: number` prop——变化即重拉（走既有 gen 代际守卫，θ 重拉同款）；fetch 404 文案区分「暂无明细（未完成或老 run）」。
- **文档**：`docs/eval.md` 增「实时进度明细」一节（live.json 产物 + 回落序 + 视图行为）。

## 任务草案（2 任务）

1. T1 后端（行构造 helper 抽取 + 起跑空 live.json + `_publish_live` + 收敛循环挂点 + 终态删除 + `_movies_read` 回落 + 测试）；
2. T2 前端（路由扩展 + 进度明细视图/横幅 + refreshTick 轮询 + 终态自动切报告 + 测试）。
