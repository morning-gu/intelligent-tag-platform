// Prompt 模板只读契约视图 API（四层重构 Task 3 后端定稿、Task 14 前端同步）：
// 文案是代码常量（tagplatform.capabilities.prompts.templates，git 即版本
// 机制），prompt_template 表已随迁移 0018 drop——DB 版本机制（POST 新版本 /
// activate 切换）随之删除，本模块只提供 GET 清单。返回项兼作契约单一来源：
// 前端配置页只读展示（模板文本 + 占位符白名单/必需项动态渲染，不手写文案），
// 生成流水线与测试按同一契约渲染。写端点已不存在：POST /prompts → 405、
// POST /prompts/{id}/activate → 404（契约锁定测试钉死于本模块 prompts.test.ts
// 与后端 test_api_prompts_admin）。类型形状与后端
// tagplatform/api/routers/prompts.py 的 PromptContractOut 一致。
import { apiFetch } from './client'

/** Prompt 只读契约项（与后端 PromptContractOut 一致）：mode + 模板正文 +
 * 占位符白名单/必需项。无版本字段——git 即版本，DB 版本机制已删除。 */
export interface PromptContractItem {
  mode: string
  template_text: string
  allowed_fields: string[]
  required_placeholders: string[]
}

/** Prompt 模板只读清单：mode 精确过滤（closed|open），缺省返回全部 mode，
 * 后端按 mode 排序；正文与后端本地常量逐字一致。 */
export function fetchPrompts(mode?: string): Promise<PromptContractItem[]> {
  const query = mode === undefined ? '' : `?mode=${encodeURIComponent(mode)}`
  return apiFetch<PromptContractItem[]>(`/api/v1/prompts${query}`)
}

/** 流水线文案清单条目（与后端 RegistryItemOut 一致）：常量名 + 文案本体。 */
export interface PromptRegistryItem {
  name: string
  text: string
}

/** 流水线文案清单组（与后端 RegistryGroupOut 一致）：按流水线分组
 * （closed/mtg/assets/quality），文案与 capabilities/prompts 各模块常量
 * 单一来源逐字一致。 */
export interface PromptRegistryGroup {
  module: string
  title: string
  items: PromptRegistryItem[]
}

/** 全量 Prompt 文案只读清单（2026-11-22 文案集中新增）：一处看全部
 * 文案——GET /api/v1/prompts/registry 四组分组返回。 */
export function fetchPromptRegistry(): Promise<PromptRegistryGroup[]> {
  return apiFetch<PromptRegistryGroup[]>('/api/v1/prompts/registry')
}
