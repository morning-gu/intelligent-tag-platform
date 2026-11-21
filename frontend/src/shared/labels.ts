// 跨页面共享展示常量（Task 14 单一来源）：来源（origin=mode）词表、
// 挂载粒度词表与审核状态三态词表。此前 ReviewCenter / AssetDetail /
// GenerationTasks 各持本地副本（其中 GenerationTasks 的 MODE_* 与
// ORIGIN_* 是同值双份），现统一收口于此——权威取值以 ReviewCenter
// 历史版本为准（三处原本取值一致，无差异对齐）。
// 页面专属的状态映射（任务状态、导入批次状态、标签生命周期、闭环评测
// run 状态等）不在共享范围，仍留在各自页面。

/** 来源中文展示（origin 即生成模式：closed 闭环 / open 开放）。 */
export const ORIGIN_LABELS: Record<string, string> = { closed: '闭环', open: '开放' }

/** 来源 Tag 颜色（沿 v1 模式列配色：闭环蓝 / 开放紫）。 */
export const ORIGIN_COLORS: Record<string, string> = { closed: 'blue', open: 'purple' }

/** 挂载粒度中文展示（目录 applicable_scope 的落库结果）。 */
export const TARGET_TYPE_LABELS: Record<string, string> = {
  content: '整片',
  scene: '单场景',
  character: '人物',
}

/** 审核状态中文展示（键集与后端契约固定枚举一致；merged 已退役）。 */
export const REVIEW_STATUS_LABELS: Record<string, string> = {
  pending: '待审核',
  approved: '已通过',
  rejected: '已拒绝',
}

/** 审核状态 Tag 颜色（pending 金 / approved 绿 / rejected 红）。 */
export const REVIEW_STATUS_COLORS: Record<string, string> = {
  pending: 'gold',
  approved: 'green',
  rejected: 'red',
}

/** 审核状态下拉兜底（filters 接口不可用时保持页面可用）。 */
export const REVIEW_STATUS_FALLBACK = ['pending', 'approved', 'rejected']

/** 来源下拉兜底（origins 为契约固定枚举，filters 失败时仍可筛）。 */
export const ORIGIN_FALLBACK = ['closed', 'open']
