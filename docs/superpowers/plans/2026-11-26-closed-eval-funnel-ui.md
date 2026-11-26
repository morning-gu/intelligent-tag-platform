# 闭环评测整体漏斗与漏斗 UI 升级实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 「单片明细」区顶部加全 run 聚合的整体漏斗块，单片明细的文本漏斗条升级为 mini BarChart（视觉对齐开放评测报告档位）。

**Architecture:** 纯前端——聚合 `payload.rows` 各行 `funnel`（Task 3 恒写键），零后端零新请求；共用 `FunnelBars` 小组件（组合既有 `BarChart`）承载整体块与单片条；θ 门/代际守卫/懒加载缓存逻辑不动。

**Tech Stack:** React 18 + antd v6（Statistic/Table）+ 仓库既有 `BarChart.tsx`（纯 CSS 条形图）+ vitest。

**Spec:** `docs/superpowers/specs/2026-11-26-closed-eval-funnel-ui-design.md`（短设计，已批准）

## Global Constraints

- 后端零改动：report.json/端点/schema/TS api 模块（`api/eval.ts`）全不动。
- 不新增请求：聚合在已拉回的 summary payload 上（`payload.rows`）。
- 图表代码只**组合**既有 `BarChart`（`BarItem {label, value, max?, extra?}`）与 antd `Statistic`——不引入新图表库、不写新图表原语（仓库视觉语言）。
- None≠0「暂无」纪律：老 run（行全 funnel=null）整体块渲染说明文案（`run-funnel-absent`，同单片 `detail-trace-absent` 口径，显式优于消失）。
- θ 门交互不动（漏斗与 θ 无关）；既有测试的断言升级只允许「文本断言换 BarChart 行断言」这一类（升级不放松）。
- 提交信息中文 conventional 前缀（feat/）。
- 测试命令：组件级 `cd frontend && npx vitest run src/components/ClosedEvalMovieTable.test.tsx`；全量 `cd frontend && npx vitest run`；类型 `cd frontend && npx tsc -b`。

## Review Focus

1. **老 run 全 funnel=null** → 整体块出 absent 文案，不出 NaN/0 假数据。测试 → T1（老 run 夹具断言 `run-funnel-absent` 与文案）。
2. **混合态**（成功片有 funnel、失败片 funnel=null 同表）→ 聚合只计非 null 行，「合计覆盖 N/共 M 部」标注真实差值。测试 → T1（夹具两成功片+一失败片，断言聚合值只含成功片、覆盖 2/3）。
3. **召回=0 的片**（存活率分母零）→ 跳过；全部召回=0 → 存活率「暂无」。测试 → T1（夹具一 recall=0 片，断言不进 min/中位/max）。
4. **切 θ 重拉后整体块不漂移**（funnel 与 θ 无关）→ θ 切换后 `run-funnel` 仍在且数值不变。测试 → T1（切 θ 后重断言）。
5. **单片召回极小**（0 或 1-2）→ BarChart 行兜底不除零（组件内置全 0→1 兜底）。测试 → T2（单片召回 0 的明细断言五行 bar-row 渲染、无 NaN）。

---

### Task 1: 整体漏斗块（RunFunnelSummary + FunnelBars + 聚合纯函数）

**Files:**
- Modify: `frontend/src/components/ClosedEvalMovieTable.tsx`（新增 `FunnelBars`/`aggregateFunnel`/`survivalStats`/`RunFunnelSummary`，主 return 在 θ 控件 div 与主表 `<Table>` 之间插入）
- Test: `frontend/src/components/ClosedEvalMovieTable.test.tsx`（新增用例 + 夹具扩展）

**Interfaces:**
- Consumes: `payload.rows: MovieSummaryRow[]`（行恒带 `funnel: FunnelCounts | null`，`FunnelCounts` 十键 number）；`BarItem`（`BarChart.tsx` 既有）。
- Produces（T2 依赖）:
  - `FunnelBars({ counts }: { counts: FunnelCounts }): JSX.Element`——五行 BarChart（召回/提及/门控过/挂载成/产出；「门控过 = n_mentioned − n_gate」「挂载成 = 门控过 − n_unmountable」差式派生，与现 `FunnelBar` 同款；显式 `max = counts.n_recall`）
  - `aggregateFunnel(rows: MovieSummaryRow[]): { counts: FunnelCounts; n_movies: number } | null`——十键逐键加总非 null 行；无任何非 null 行 → null
  - `survivalStats(rows: MovieSummaryRow[]): { min: number; median: number; max: number } | null`——每片 n_kept/n_recall（召回=0 跳过；偶数个取中间两数均值；无 eligible → null）

- [ ] **Step 1: 写失败测试（新节「整体漏斗块」）**

```tsx
// 夹具：SUMMARY 加一行 funnel 非 null 的行（或复用现有两行有 funnel 的夹具，
// 另备老 run 夹具 rows 全 funnel:null、失败片混入夹具、召回 0 片夹具）
it('整体漏斗块：Statistic 五值 + BarChart 五行 + 次要计数 + 存活率 + 覆盖标注', () => {
  // mock getRunMovies 返回含 funnel 的 rows → 断言 data-testid="run-funnel"：
  // 召回/提及/门控过/挂载成/产出五 Statistic（聚合值与差式派生值精确相等）；
  // bar-row-召回…bar-row-产出 存在且数值对；次要计数行含 门控丢弃/挂载失败/
  // 上限截断/编造/批次（解析重试）；「合计覆盖 2 部 / 共 3 部」；
  // 每片存活率 min/中位/max 手算精确断言
})
it('老 run：整体块渲染说明文案', () => {
  // rows 全 funnel:null → data-testid="run-funnel-absent" +
  // 文案含「本 run 早于管线追踪功能」；不渲染 run-funnel 统计内容
})
it('失败片混入：聚合只计非 null 行，覆盖 N/M 真实', () => { /* 覆盖 2/3 */ })
it('召回 0 的片不进存活率；全部召回 0 → 存活率「暂无」', () => {})
it('切 θ 重拉后整体块数值不变', () => {
  // changeTheta 触发重拉（mock 第二次返回同 funnel 不同指标行）→ run-funnel 聚合值不变
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/components/ClosedEvalMovieTable.test.tsx`
Expected: FAIL（`run-funnel` 找不到）

- [ ] **Step 3: 实现**

`FunnelBars`（五行 `BarItem`，label 中文同现文案、显式 max=n_recall）→ `aggregateFunnel`/`survivalStats` 纯函数（组件外导出或文件内导出供测试）→ `RunFunnelSummary({ rows })`：`aggregateFunnel` 为 null → `run-funnel-absent` 文案；否则 `run-funnel` 内 `Statistic` 卡行五值（`run-funnel-recall` 等 testid）+ `FunnelBars` + 次要计数 Typography 行（门控丢弃/挂载失败/上限截断/编造/批次（解析重试）——**不含**输出上限：聚合的 max_tags 无意义）+ 存活率行（null → 「暂无」）+ 覆盖标注。主 return 在 θ 控件 div 之后、`<Table>` 之前插入 `<RunFunnelSummary rows={payload.rows} />`。中位数偶数取中间两数均值。

- [ ] **Step 4: 跑测试 + 全量**

Run: `cd frontend && npx vitest run src/components/ClosedEvalMovieTable.test.tsx && npx vitest run && npx tsc -b`
Expected: 全 PASS / tsc 0 错

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/ClosedEvalMovieTable.tsx frontend/src/components/ClosedEvalMovieTable.test.tsx
git commit -m "feat: 单片明细区整体漏斗块（聚合/存活率纯函数+Statistic+BarChart，老 run 暂无口径）"
```

### Task 2: 单片漏斗条升级 mini BarChart

**Files:**
- Modify: `frontend/src/components/ClosedEvalMovieTable.tsx`（`FunnelBar` 110-133 行改造）
- Test: `frontend/src/components/ClosedEvalMovieTable.test.tsx`（既有 detail-funnel 用例断言升级）

**Interfaces:**
- Consumes: Task 1 的 `FunnelBars({ counts })`。
- Produces: 无（叶子改动）。

- [ ] **Step 1: 升级既有用例断言（文本断言 → BarChart 行断言）**

现有「明细漏斗条渲染五计数」用例的主链文本断言（`召回 X → 提及 Y …`）替换为：`detail-funnel` 内 `bar-row-召回`…`bar-row-产出` 五行存在、数值精确（差式派生值正确）；次要计数文本行断言**原样保留**（含 输出上限——单片 M 有意义）；新增一条：召回 0 的单片明细五行 bar-row 渲染无 NaN（BarChart 全 0 兜底）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/components/ClosedEvalMovieTable.test.tsx`
Expected: FAIL（文本断言已删、bar-row 未渲染）

- [ ] **Step 3: 实现 `FunnelBar` 改造**

保留 `data-testid="detail-funnel"`、`Typography.Title`「管线漏斗」、次要计数行（含输出上限）；主链文本行替换为 `<FunnelBars counts={counts} />`。差式派生逻辑随 FunnelBars 内聚（FunnelBar 不再自算）。

- [ ] **Step 4: 跑测试 + 全量**

Run: `cd frontend && npx vitest run src/components/ClosedEvalMovieTable.test.tsx && npx vitest run && npx tsc -b`
Expected: 全 PASS / tsc 0 错

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/ClosedEvalMovieTable.tsx frontend/src/components/ClosedEvalMovieTable.test.tsx
git commit -m "feat: 单片漏斗条升级 mini BarChart（复用 FunnelBars，次要计数保留）"
```
