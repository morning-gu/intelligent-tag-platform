// 运行配置 API（阶段 4 Task 2 契约）：GET /api/v1/config 只读视图（admin）。
// 类型形状与后端 tagplatform/api/routers/config.py 的 ConfigOut 一一对应——
// 十三字段白名单，机密（session_secret/admin_password）绝不入列。只读语义：
// 后端不提供任何写配置端点，修改走环境变量 + 重启服务（见 deploy/deploy.md）。
import { apiFetch } from './client'

/** 运行配置只读视图（与后端 ConfigOut 字段一一对应）。 */
export interface RuntimeConfig {
  llm_model: string
  llm_temperature: number
  /** 开放流水线 judge 模型（mtg_judge_model）。 */
  mtg_judge_model: string
  /** judge 开关（关掉则只合并不过滤）。 */
  mtg_judge_enabled: boolean
  /** 嵌入后端：local | remote。 */
  embedding_backend: string
  /** 嵌入模型名或本地模型目录。 */
  embedding_model: string
  /** 开放评测评审模型；空串 = 回落主 LLM（同模评审）。 */
  open_eval_review_model: string
  recall_top_k_per_type: number
  open_match_threshold: number
  generation_workers: number
  use_mock_llm: boolean
  use_mock_embedder: boolean
  database_path: string
}

/** 只读运行配置（仅 admin，非 admin 后端 403）。 */
export function fetchRuntimeConfig(): Promise<RuntimeConfig> {
  return apiFetch<RuntimeConfig>('/api/v1/config')
}
