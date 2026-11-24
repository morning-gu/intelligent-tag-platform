# open-eval（开放评测）

四层重构规划中的第七个页面模块：开放评测。页面 `OpenEval.tsx`（四块视图：
创建表单/进行中卡片/历史表/报告卡片）+ 随模块测试 `OpenEval.test.tsx`；
API 契约在 `src/api/evalOpen.ts`；报告视图组件（Task 8）落
`src/components/OpenEvalReport.tsx`，逐片明细内层「生产审计」块落
`src/components/OpenEvalMovieAudit.tsx`（展开时懒加载单片诊断端点，
0 条产出的影片也能看到候选去向）。

约定：与 tag-management / content-management / generation / review /
closed-eval / config 模块并列；跨模块共享组件仍在 `src/components/`，
跨页面常量仍在 `src/shared/`。
