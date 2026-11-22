// 开放评测 API：/eval/open 路由族的类型契约与取数函数（open-eval Task 7）。
// 端点数以后端 eval.py 路由模块 docstring 为准，不在此重复计数——固定数字
// 曾随端点增加而过时。类型形状与后端 tagplatform/api/routers/eval.py 的
// Pydantic 模型、open_service 组装的 run 行、open_report.assemble_open_report
// 组装的 report.json 结构一致：后端 None（无报告/该 θ 无有效影片/无可标定
// 片/算不出）一律以 null 透传，前端按「暂无」口径呈现（None≠0 纪律）；起跑
// 可选项缺省即不携带，由服务端回退默认网格/模态/并发。dataset 三端点与
// 闭环共享 override 状态（写的是同一个 override），本模块仍提供独立封装——
// 路径语义清晰、两页互不 import。产物文件下载走 <a href> 原生下载，
// openFileUrl 只拼路径不请求——同源导航自带会话 cookie，且文件是二进制/
// 文本流而非 JSON，apiFetch 的 response.json() 不适用。
import { apiFetch } from './client'
import type {
  DatasetInfo,
  DatasetRootInfo,
  MovieRef,
  MovieSelection,
  RunStatus,
} from './eval'

// 共享形状（与闭环后端契约逐一相同）经本模块转出口，页面侧一处 import：
export type { DatasetInfo, DatasetRootInfo, MovieRef, MovieSelection, RunStatus }

/** 起跑请求体（与后端 StartOpenRunRequest 一致）：thetas/modalities/
 * concurrency 缺省 → 服务端默认（DEFAULT_THETAS=(0.2,0.4,0.6,0.8) /
 * ("script","dialogue") / 4）。modalities 词表校验在服务层（非空 ⊆
 * {script, dialogue} 且必含 script——台词在 script 通道渲染，缺 script
 * 评审材料为空；video 显式拒绝：v1 文本模态）→ 400。 */
export interface StartOpenRunRequest {
  dataset: string
  movies: MovieSelection
  thetas?: number[]
  modalities?: string[]
  concurrency?: number
}

/** 历史摘要行（与后端 OpenRunSummaryOut 一致）：基础行字段与闭环同形
 * （done/total/failed/started_at/finished_at/error 允许 null——磁盘兜底行
 * 宁缺勿崩）；摘要五键（accuracy40/new_pass/hit_pass/agreement40/new_ratio）
 * 由 open_report._read_open_summary 读该 run 的 report.json 出（统一取格
 * 约定：accuracy40/agreement40 = 最接近 θ0.4 的最近有值格；new_pass/
 * hit_pass/new_ratio 直读宏平均；报告缺失/截断 → 对应键 null，宁缺勿假）。
 * 闭环侧的 ndcg10/p10/r20/coverage_median 不在开放口径内，不设字段。 */
export interface OpenRunSummary {
  run_id: string
  dataset_id: string | null
  status: string
  done: number | null
  total: number | null
  failed: number | null
  started_at: string | null
  finished_at: string | null
  error: string | null
  accuracy40: number | null
  new_pass: number | null
  hit_pass: number | null
  agreement40: number | null
  new_ratio: number | null
}

/** θ 网格单行（report.json theta_rows 行，与 open_ranking.ThetaRow 一致）：
 * 指标 null 表示该 θ 无有效影片/无可标定片（None≠0）；CI 为逐样本自举
 * 百分位区间 [lo, hi]；随机/先验基线同行给出（与闭环 report 的 baselines
 * 分表不同——开放侧基线就在 θ 格内，可比性同源）。 */
export interface OpenThetaRow {
  theta: number
  n_valid: number
  accuracy: number | null
  accuracy_ci: [number, number] | null
  accuracy_random: number | null
  accuracy_prior: number | null
  coverage: number | null
  n_calibrated: number
  agreement: number | null
  agreement_ci: [number, number] | null
  fp_rate: number | null
  fn_rate: number | null
}

/** 逐片折叠明细行（report.json movies 行）：review 三态 ok/failed/skipped
 * （failed=评审失败、skipped=produce 失败未发起评审）；accuracy 为片级
 * 集合准确率@θ0.4 取格（交集内命中为空 → null，None≠0）；new_pass/hit_pass
 * 为片级评审通过率（评审失败片整体 null，无可判定条目 → null）。 */
export interface OpenMovieRow {
  tt: string
  title: string
  ok: boolean
  review: string
  hits_n: number
  new_n: number
  accuracy: number | null
  new_pass: number | null
  hit_pass: number | null
  error: string | null
  review_error: string | null
  elapsed_ms: number
}

/** 开放评测报告（GET /runs/{id}/report，report.json 原样透出，与后端
 * assemble_open_report 结构一致，键名逐一镜像 open_report.py，改动前先读
 * 该模块的组装代码）：banner 为 **list[str]**（开放侧把闭环的单条扩成多条
 * 聚合——部分运行/同模评审/小样本，全量干净 run → 空数组）；warnings 为
 * 口径警示列表；note 为银标准警示原文（genome relevance 是模型输出非人工
 * 标注），展示时恒不可省。sample_accounting 分四块核算（produce 失败/
 * 评审失败/交集内命中为空分开点名）。 */
export interface OpenEvalReport {
  banner: string[]
  sample_accounting: {
    total: number
    ok: number
    failed: number
    review_failed: number
    invalid: number
  }
  no_gt: number
  theta_rows: OpenThetaRow[]
  pass_rates: {
    new_macro: number | null
    new_micro: number | null
    new_n: number
    hit_macro: number | null
    hit_micro: number | null
    hit_n: number
    review_missing: number
  }
  ndcg: { macro: number | null; ci: [number, number] | null }
  new_ratio: { macro: number | null }
  produced_stats: {
    counts: { tt: string; hits_in: number; hits_out: number; new_words: number }[]
    min: number | null
    median: number | null
    max: number | null
  }
  judge: {
    self_filter: number | null
    verdicts: Record<string, number>
    dropped: Record<string, number>
    bands: Record<string, number>
  }
  calibre: {
    llm_model: string
    embedding_model: string
    prompt_version: string | null
    review_model: string | null
    review_same_model: boolean | null
    review_prompt_version: string | null
    library_version: string | null
    dataset_id: string | null
    gt_sha8: string | null
    modalities: string[] | null
  }
  movies: OpenMovieRow[]
  failed_movies: { tt: string; error: string }[]
  review_failed_movies: { tt: string; review_error: string }[]
  warnings: string[]
  note: string
}

/** 产物文件名（后端 files 端点白名单五件套；report.json 有专用 /report
 * 端点、不在白名单——结构化读走 getOpenReport，文件端点只供下载原始产物）。 */
export type OpenFileName =
  | 'report.md'
  | 'metrics.csv'
  | 'raw_results.json'
  | 'meta.json'
  | 'progress.jsonl'

/** 可评数据集列表（与闭环同状态：读路径走共享 effective_root；overlap 对
 * 当前标签库）。 */
export function listOpenDatasets(): Promise<DatasetInfo[]> {
  return apiFetch<DatasetInfo[]>('/api/v1/eval/open/datasets')
}

/** 数据集的 usable 影片清单（选片列表用）；未知 dataset → 404。 */
export function listOpenMovies(datasetId: string): Promise<MovieRef[]> {
  return apiFetch<MovieRef[]>(`/api/v1/eval/open/datasets/${datasetId}/movies`)
}

/** 当前生效的数据集根（开放评测与闭环共享同一 override 状态：页面「当前
 * 生效」展示的服务端真相）。 */
export function getOpenDatasetRoot(): Promise<DatasetRootInfo> {
  return apiFetch<DatasetRootInfo>('/api/v1/eval/open/dataset-root')
}

/** 设置数据集根（写的是闭环服务的同一 override，重启回落 .env 默认）。校验
 * 失败 → 400（目录不存在/非目录/扫描零有效数据集，detail 经
 * ApiError.message 透出）；返回应用后的生效值与 is_default。 */
export function setOpenDatasetRoot(root: string): Promise<DatasetRootInfo> {
  return apiFetch<DatasetRootInfo>('/api/v1/eval/open/dataset-root', {
    method: 'POST',
    body: JSON.stringify({ root }),
  })
}

/** 起跑：返回受理 run_id（进度经 listOpenRuns 轮询）。错误映射：未知
 * dataset → 404、校验失败（选片越权/模态词表/θ 越界/库未就绪）→ 400、
 * 已有活跃 run → 409（含跨服务互斥——closed 活跃时 detail 带对端 run_id，
 * 均经 ApiError.message 透出后端 detail）。 */
export function startOpenRun(req: StartOpenRunRequest): Promise<{ run_id: string }> {
  return apiFetch<{ run_id: string }>('/api/v1/eval/open/runs', {
    method: 'POST',
    body: JSON.stringify(req),
  })
}

/** 开放评测历史 run 摘要（磁盘扫描，started_at 降序；摘要五键无报告 → null）。 */
export function listOpenRuns(): Promise<OpenRunSummary[]> {
  return apiFetch<OpenRunSummary[]>('/api/v1/eval/open/runs')
}

/** run 进度详情：注册表优先（活值）、磁盘历史兜底（上一进程的 run/中断
 * run）；两处都没有 → 404。 */
export function getOpenRun(runId: string): Promise<RunStatus> {
  return apiFetch<RunStatus>(`/api/v1/eval/open/runs/${runId}`)
}

/** 请求取消 → ok：受理 true（worker 不再提交新片，在途片跑完）；run
 * 不存在或已终态 → false（幂等意图，重复取消不报错）。 */
export function cancelOpenRun(runId: string): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>(`/api/v1/eval/open/runs/${runId}/cancel`, {
    method: 'POST',
  })
}

/** 删除终态 run（磁盘产物 + 进程内注册表）→ {ok: true}；运行中（含终态
 * 落盘窄窗口）→ 409 detail 带原因；不存在/已删 → 404（重复删除即 404）。
 * 经 ApiError.message 透出后端 detail。 */
export function deleteOpenRun(runId: string): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>(`/api/v1/eval/open/runs/${runId}`, {
    method: 'DELETE',
  })
}

/** 报告 JSON（report.json 原样透出）：无报告/截断 → 404（meta→raw→report
 * 写序的非原子窗口，读侧容错不抛 500）。 */
export function getOpenReport(runId: string): Promise<OpenEvalReport> {
  return apiFetch<OpenEvalReport>(`/api/v1/eval/open/runs/${runId}/report`)
}

/** 产物下载路径（白名单五件套）：纯路径拼接供 <a href>/window.open 使用，
 * 不经 apiFetch（非 JSON 响应）。 */
export function openFileUrl(runId: string, name: OpenFileName): string {
  return `/api/v1/eval/open/runs/${runId}/files/${name}`
}
