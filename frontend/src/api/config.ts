// 运行配置 API（阶段 4 Task 2 契约）：GET /api/v1/config 只读视图（admin）。
// 类型形状与后端 tagplatform/api/routers/config.py 的 ConfigOut 一一对应——
// 九字段白名单，机密（session_secret/admin_password）绝不入列。只读语义：
// 后端不提供任何写配置端点，修改走环境变量 + 重启服务（见 deploy/deploy.md）。
import { apiFetch } from './client'

/** 运行配置只读视图（与后端 ConfigOut 字段一一对应）。 */
export interface RuntimeConfig {
  llm_model: string
  llm_temperature: number
  recall_top_k_per_type: number
  open_match_threshold: number
  semantic_similar_threshold: number
  generation_workers: number
  use_mock_llm: boolean
  use_mock_embedder: boolean
  database_path: string
}

/** 只读运行配置（仅 admin，非 admin 后端 403）。 */
export function fetchRuntimeConfig(): Promise<RuntimeConfig> {
  return apiFetch<RuntimeConfig>('/api/v1/config')
}
