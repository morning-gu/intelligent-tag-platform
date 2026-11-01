// Prompt 模板版本管理 API（阶段 4 Task 2 契约，审查轮 1 增补 template_text）：
// 版本列表（mode 过滤，列表直接带正文——运营侧新建版本需基于现版微调而非
// 从零转写，正文非机密、登录可见即可）、新建版本（admin）、激活切换（admin）。
// 类型形状与后端 tagplatform/api/routers/prompts.py 的 PromptOut /
// PromptCreateRequest / PromptActivateOut 一致。历史版本不可变——后端不提供
// 编辑/删除端点，换提示词 = 追加新版本 + 切换激活。422（空文本/缺占位符）/
// 404（未知 id）/ 403（非 admin）经 client 的 ApiError 透出后端 detail。
import { apiFetch } from './client'

/** Prompt 模板列表项（与后端 PromptOut 一致，含模板正文 template_text）。 */
export interface PromptTemplateSummary {
  id: number
  mode: string
  version: number
  template_text: string
  is_active: boolean
  created_at: string | null
}

/** 新建版本请求体（与后端 PromptCreateRequest 一致）：mode 取值 closed|open
 * （之外后端 422）；template_text 合法性（strip 非空 + 必需占位符）由后端
 * 服务层校验，非法 422。 */
export interface PromptCreatePayload {
  mode: 'closed' | 'open'
  template_text: string
}

/** 激活结果（与后端 PromptActivateOut 一致）：新激活版本的 mode/version/is_active。 */
export interface PromptActivateResult {
  mode: string
  version: number
  is_active: boolean
}

/** Prompt 模板列表：mode 精确过滤（closed|open），后端按 mode+version 排序。 */
export function fetchPrompts(mode?: string): Promise<PromptTemplateSummary[]> {
  const query = mode === undefined ? '' : `?mode=${encodeURIComponent(mode)}`
  return apiFetch<PromptTemplateSummary[]>(`/api/v1/prompts${query}`)
}

/** 新建 Prompt 版本（admin）：新版本落库即激活，同 mode 旧激活行自动停用。 */
export function createPromptVersion(payload: PromptCreatePayload): Promise<PromptTemplateSummary> {
  return apiFetch<PromptTemplateSummary>('/api/v1/prompts', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/** 激活指定版本（admin）：未知 id 404；幂等（已激活原样返回）。 */
export function activatePromptVersion(promptId: number): Promise<PromptActivateResult> {
  return apiFetch<PromptActivateResult>(`/api/v1/prompts/${promptId}/activate`, {
    method: 'POST',
  })
}
