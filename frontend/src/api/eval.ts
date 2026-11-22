import { apiFetch } from './client'
import type { QualityLabel, QualityMetrics, QualityStatus } from './tasks'

/** 可评数据集视图（与后端 DatasetInfoOut 一致）：usable_movies 输出为
 * 数量（服务层是清单——列表页只要计数，选片清单走 listMovies）。 */
export interface DatasetInfo {
  id: string
  gt_path: string | null
  n_movies: number
  n_tags: number | null
  library_overlap: number | null
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
  quality_model?: string
}

/** run 进度快照（与后端 RunStateOut 一致）：done/total/failed/started_at
 * /finished_at 允许 null——磁盘兜底行（上一进程残留/损坏 meta）宁缺勿崩，
 * 不打成 500；finished_at 终态时刻，运行中/老 run → null。 */
export interface RunStatus {
  run_id: string
  status: string
  done: number | null
  total: number | null
  failed: number | null
  started_at: string | null
  finished_at: string | null
  error: string | null
}

/** 历史摘要行（与后端 RunSummaryOut 一致）：摘要四键取 θ=0.4、K=10/20 档
 * （无报告/无该档 → null，宁缺勿崩）；error 为失败留痕（终态 failed 才有）；
 * finished_at 终态时刻（老 run 无 → null）。 */
export interface RunSummary {
  run_id: string
  dataset_id: string | null
  status: string
  done: number | null
  total: number | null
  failed: number | null
  started_at: string | null
  finished_at: string | null
  error: string | null
  ndcg10: number | null
  p10: number | null
  r20: number | null
  coverage_median: number | null
  quality_coverage?: number | null
  quality_full_rate?: number | null
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

export interface OpenEvalLabel extends Omit<QualityLabel, 'disposition'> {
  level: 'film' | 'scene'
  reason: string | null
  evidence: { scene_id: string; start_ms: number; description: string }[]
}

export interface OpenEvalReport {
  meta: {
    dataset: { id: string; root: string }
    prompt: { version: string }
    llm_model: string
    quality_model: string
    quality_prompt_version: string
    mock_llm: boolean
    mock_embedder: boolean
    done: number
    total: number
  }
  incomplete: boolean
  film_counts: Partial<Record<QualityStatus, number>>
  metrics: QualityMetrics
  films: {
    tt: string
    title: string
    status: QualityStatus
    error: string | null
    note: string | null
    frame_count: number
    scene_count?: number
    metrics: QualityMetrics | null
    produced: OpenEvalLabel[]
  }[]
}

export type EvalFileName =
  | 'report.md'
  | 'report.json'
  | 'metrics.csv'
  | 'raw_results.json'
  | 'meta.json'
  | 'progress.jsonl'

export interface DatasetRootInfo {
  root: string
  is_default: boolean
}

export type EvalMode = 'closed' | 'open'

function evalApi<Report>(mode: EvalMode) {
  const base = `/api/v1/eval/${mode}`
  const runs = `${base}/runs`
  const run = (runId: string) => `${runs}/${encodeURIComponent(runId)}`
  return {
    listDatasets: () => apiFetch<DatasetInfo[]>(`${base}/datasets`),
    listMovies: (datasetId: string) =>
      apiFetch<MovieRef[]>(`${base}/datasets/${encodeURIComponent(datasetId)}/movies`),
    setDatasetRoot: (root: string) => apiFetch<DatasetRootInfo>(`${base}/dataset-root`, {
      method: 'POST',
      body: JSON.stringify({ root }),
    }),
    getDatasetRoot: () => apiFetch<DatasetRootInfo>(`${base}/dataset-root`),
    startRun: (req: StartRunRequest) => apiFetch<{ run_id: string }>(runs, {
      method: 'POST',
      body: JSON.stringify(req),
    }),
    listRuns: () => apiFetch<RunSummary[]>(runs),
    getRun: (runId: string) => apiFetch<RunStatus>(run(runId)),
    cancelRun: (runId: string) => apiFetch<{ ok: boolean }>(`${run(runId)}/cancel`, { method: 'POST' }),
    deleteRun: (runId: string) => apiFetch<{ ok: boolean }>(run(runId), { method: 'DELETE' }),
    getReport: (runId: string) => apiFetch<Report>(`${run(runId)}/report`),
    fileUrl: (runId: string, name: EvalFileName) => `${run(runId)}/files/${name}`,
  }
}

export const {
  listDatasets, listMovies, setDatasetRoot, getDatasetRoot, startRun, listRuns,
  getRun, cancelRun, deleteRun, getReport, fileUrl,
} = evalApi<ClosedEvalReport>('closed')

export const openEvalApi = evalApi<OpenEvalReport>('open')
