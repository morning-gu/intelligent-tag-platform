// 闭环评测 API：/eval/closed 八端点的类型契约与取数函数。类型形状与后端
// tagplatform/api/routers/eval.py 的 Pydantic 模型、closed_eval 组装的
// report.json 结构一致：后端 None（无报告/该档无有效影片/算不出）一律以
// null 透传，前端按「暂无」口径呈现（None≠0 纪律）；起跑可选项缺省即不
// 携带，由服务端回退默认网格/并发。产物文件下载走 <a href> 原生下载，
// fileUrl 只拼路径不请求——同源导航自带会话 cookie，且文件是二进制/文本
// 流而非 JSON，apiFetch 的 response.json() 不适用。
import { apiFetch } from './client'

/** 可评数据集视图（与后端 DatasetInfoOut 一致）：usable_movies 输出为
 * 数量（服务层是清单——列表页只要计数，选片清单走 listMovies）。 */
export interface DatasetInfo {
  id: string
  gt_path: string
  n_movies: number
  n_tags: number
  library_overlap: number
  usable_movies: number
}

/** 影片引用（选片列表条目，与后端 MovieRefOut 一致）。 */
export interface MovieRef {
  tt: string
  title: string
}

/** 选片请求：mode 三态（preset30 固定 seed 抽样 / all / custom 显式清单），
 * ids 仅 custom 需要（越权/为空由服务层校验 → 400）。 */
export interface MovieSelection {
  mode: 'preset30' | 'all' | 'custom'
  ids?: string[]
}

/** 起跑请求体：thetas/ks/concurrency 缺省 → 服务端默认网格/并发
 * （前端仅在用户填写时逐字段携带）。 */
export interface StartRunRequest {
  dataset: string
  movies: MovieSelection
  thetas?: number[]
  ks?: number[]
  concurrency?: number
}

/** run 进度快照（与后端 RunStateOut 一致）：done/total/failed/started_at
 * 允许 null——磁盘兜底行（上一进程残留/损坏 meta）宁缺勿崩，不打成 500。 */
export interface RunStatus {
  run_id: string
  status: string
  done: number | null
  total: number | null
  failed: number | null
  started_at: string | null
  error: string | null
}

/** 历史摘要行（与后端 RunSummaryOut 一致）：摘要四键取 θ=0.4、K=10/20 档
 * （无报告/无该档 → null，宁缺勿崩）；error 为失败留痕（终态 failed 才有）。 */
export interface RunSummary {
  run_id: string
  dataset_id: string | null
  status: string
  done: number | null
  total: number | null
  failed: number | null
  started_at: string | null
  error: string | null
  ndcg10: number | null
  p10: number | null
  r20: number | null
  coverage_median: number | null
}

/** θ×K 网格单格（report.json grid 行）：指标 null 表示该 θ 下无有效影片
 * （None≠0）；CI 为逐样本自举百分位区间 [lo, hi]。 */
export interface GridRow {
  theta: number
  k: number
  n_valid: number
  precision: number | null
  recall: number | null
  ndcg: number | null
  precision_ci: [number, number] | null
  recall_ci: [number, number] | null
  ndcg_ci: [number, number] | null
  precision_random: number | null
  recall_random: number | null
}

/** 闭环评测报告（GET /runs/{id}/report，report.json 原样透出，与后端
 * assemble_report 结构一致）：banner/warning 为部分口径提示（全量 →
 * null）；先验基线与系统吃同一指标管线，格值可比；note 为银标准警示
 * （genome relevance 是模型输出非人工标注），展示时恒不可省。 */
export interface ClosedEvalReport {
  banner: string | null
  sample_accounting: {
    total: number
    ok: number
    failed: number
    invalid: number
    no_gt: number
  }
  grid: GridRow[]
  baselines: { prior: GridRow[] }
  coverage: {
    min: number | null
    median: number | null
    max: number | null
    values: number[]
  }
  spearman: {
    macro: number | null
    per_movie: { tt: string; spearman: number | null }[]
  }
  failed_movies: { tt: string; error: string }[]
  warning: string | null
  calibre: {
    llm_model: string
    embedding_model: string
    prompt_version: string | null
    library_version: string | null
    dataset_id: string | null
    gt_sha8: string | null
  }
  note: string
}

/** 产物文件名（后端 files 端点白名单五件套；report.json 有专用 /report
 * 端点、不在白名单——结构化读走 getReport，文件端点只供下载原始产物）。 */
export type EvalFileName =
  | 'report.md'
  | 'metrics.csv'
  | 'raw_results.json'
  | 'meta.json'
  | 'progress.jsonl'

/** 可评数据集列表（畸形 gt 已在扫描层跳过；overlap 对当前标签库）。 */
export function listDatasets(): Promise<DatasetInfo[]> {
  return apiFetch<DatasetInfo[]>('/api/v1/eval/closed/datasets')
}

/** 数据集的 usable 影片清单（选片列表用）；未知 dataset → 404。 */
export function listMovies(datasetId: string): Promise<MovieRef[]> {
  return apiFetch<MovieRef[]>(`/api/v1/eval/closed/datasets/${datasetId}/movies`)
}

/** 当前生效的数据集根（override ?? .env 默认）与是否默认（重启后
 * override 消失 → 回落默认、is_default=true）。 */
export interface DatasetRootInfo {
  root: string
  is_default: boolean
}

/** 设置数据集根（进程内 override，重启回落 .env 默认）。校验失败 → 400
 * （目录不存在/非目录/扫描零有效数据集，detail 经 ApiError.message 透出）；
 * 返回应用后的生效值与 is_default。 */
export function setDatasetRoot(root: string): Promise<DatasetRootInfo> {
  return apiFetch<DatasetRootInfo>('/api/v1/eval/closed/dataset-root', {
    method: 'POST',
    body: JSON.stringify({ root }),
  })
}

/** 当前生效的数据集根（页面「当前生效」展示的服务端真相）。 */
export function getDatasetRoot(): Promise<DatasetRootInfo> {
  return apiFetch<DatasetRootInfo>('/api/v1/eval/closed/dataset-root')
}

/** 起跑：返回受理 run_id（进度经 getRun 轮询）。错误映射：未知 dataset
 * → 404、校验失败（选片越权/库未就绪/零交集/并发非法）→ 400、已有活跃
 * run → 409（detail 含占用的 run_id，均经 ApiError.message 透出后端 detail）。 */
export function startRun(req: StartRunRequest): Promise<{ run_id: string }> {
  return apiFetch<{ run_id: string }>('/api/v1/eval/closed/runs', {
    method: 'POST',
    body: JSON.stringify(req),
  })
}

/** 历史 run 摘要（磁盘扫描，started_at 降序；摘要档无 → null）。 */
export function listRuns(): Promise<RunSummary[]> {
  return apiFetch<RunSummary[]>('/api/v1/eval/closed/runs')
}

/** run 进度详情：注册表优先（活值）、磁盘历史兜底（上一进程的 run/
 * 中断 run）；两处都没有 → 404。 */
export function getRun(runId: string): Promise<RunStatus> {
  return apiFetch<RunStatus>(`/api/v1/eval/closed/runs/${runId}`)
}

/** 请求取消 → ok：受理 true（worker 不再提交新片，在途片跑完）；run
 * 不存在或已终态 → false（幂等意图，重复取消不报错）。 */
export function cancelRun(runId: string): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>(`/api/v1/eval/closed/runs/${runId}/cancel`, {
    method: 'POST',
  })
}

/** 报告 JSON（report.json 原样透出）：无报告/截断 → 404（meta→raw→report
 * 写序的非原子窗口，读侧容错不抛 500）。 */
export function getReport(runId: string): Promise<ClosedEvalReport> {
  return apiFetch<ClosedEvalReport>(`/api/v1/eval/closed/runs/${runId}/report`)
}

/** 产物下载路径（白名单五件套）：纯路径拼接供 <a href>/window.open 使用，
 * 不经 apiFetch（非 JSON 响应）。 */
export function fileUrl(runId: string, name: EvalFileName): string {
  return `/api/v1/eval/closed/runs/${runId}/files/${name}`
}
