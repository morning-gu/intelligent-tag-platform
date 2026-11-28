// 开放评测 API：/eval/open 路由族的类型契约与取数函数（open-eval Task 7）。
// 端点数以后端 eval.py 路由模块 docstring 为准，不在此重复计数——固定数字
// 曾随端点增加而过时。类型形状与后端 tagplatform/api/routers/eval.py 的
// Pydantic 模型、open_service 组装的 run 行、open_report.assemble_open_report
// 组装的 report.json 结构一致：后端 None（无报告/评审失败/算不出）一律以 null 透传，前端按「暂无」口径呈现（None≠0 纪律）；起跑
// 可选项缺省即不携带，由服务端回退默认模态/并发。dataset 三端点与
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

/** 起跑请求体（与后端 StartOpenRunRequest 一致）：modalities/concurrency
 * 缺省 → 服务端默认（("script","dialogue") / 4）。modalities 词表校验在
 * 服务层（非空 ⊆ {script, dialogue} 且必含 script——台词在 script 通道
 * 渲染，缺 script 评审材料为空；video 显式拒绝：v1 文本模态）→ 400。
 * 开放评测无 gt 排序任务，不设 thetas（闭环遗留口径已移除）。 */
export interface StartOpenRunRequest {
  dataset: string
  movies: MovieSelection
  modalities?: string[]
  concurrency?: number
}

/** 历史摘要行（与后端 OpenRunSummaryOut 一致）：基础行字段与闭环同形
 * （done/total/failed/started_at/finished_at/error 允许 null——磁盘兜底行
 * 宁缺勿崩）；摘要五键（pass_rate/new_pass/hit_pass/new_ratio/three_high）
 * 由 open_report._read_open_summary 直读 report.json 的 summary 出（报告
 * 缺失/截断 → 对应键 null，宁缺勿假）。three_high = 三维同时满分率。 */
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
  pass_rate: number | null
  new_pass: number | null
  hit_pass: number | null
  new_ratio: number | null
  three_high: number | null
}

/** 标签价值单方向评分（内容呈现 / 内容搜索 / 内容推荐）。 */
export interface ValueDirectionScore {
  score: number | null
  reason: string
}

/** 单维评分（评审层回填）：score 0-4（value 为加权两位小数）或 null（无法判断）；reason 简短理由。 */
export interface DimensionScore {
  score: number | null
  reason: string
  tag_kind?: string
  weights?: Record<string, number>
  dimensions?: Record<'presentation' | 'search' | 'recommendation', ValueDirectionScore>
}

/** 标签评审结果。pass 语义随 calibre.review_config.pass_mode：threshold
 * = 程序按三维分阈值复算（评审归一，模型只出分不出保留意见）；model =
 * 旧协议模型直出。行契约恒 9 键——quote/confidence 为评审归一（Task 8）
 * 新增，部分漏判失败行与旧 run 缺键 → undefined。 */
export interface TagReview {
  pass: boolean | null
  relevance: DimensionScore
  semantics: DimensionScore
  value: DimensionScore
  reason: string
  evaluated: boolean
  evidence_scene_ids: string[]
  quote?: string | null
  confidence?: number | null
}

/** 生产 judge 三维参考分（审计缺失时整条 null）：仅供参考，评审独立判断。 */
export interface JudgeReference {
  relevance: number | null
  semantics: number | null
  value: number | null
  value_scores?: Record<string, number>
  tag_kind?: string
  reason: string
  quote?: string | null
}

/** 历史评测可能缺少部分挂载字段。 */
export interface OpenLabelMount {
  dir_id?: string | null
  dir_name?: string | null
  target_type?: string | null
}

/** 库内命中行（tag_id 非空的产出）。 */
export interface OpenHitRow extends OpenLabelMount {
  tag_id: string
  tag_name: string
  desc: string | null
  level: string | null
  scene_ids?: string[]
  judge_reference: JudgeReference | null
  review: TagReview | null
}

/** 新词行（tag_id 为空的产出）；desc 为生成侧通用释义。 */
export interface OpenNewWordRow extends OpenLabelMount {
  name: string
  desc: string | null
  level: string
  scene_ids?: string[]
  judge_reference: JudgeReference | null
  review: TagReview | null
}

/** 单维汇总（与后端 tag_quality.DimensionMetrics 一致）。 */
export interface DimensionMetrics {
  evaluated_tags: number
  average: number | null
  high_rate: number | null
  distribution: number[]
  undetermined: number
  undetermined_rate: number | null
}

/** 三维汇总指标（与后端 tag_quality.QualityMetrics 一致，max_score=4）。 */
export interface QualityMetrics {
  total_tags: number
  evaluated_tags: number
  max_score: number | null
  coverage: number | null
  joint_high_rate: number | null
  three_dimension_high_rate: number | null
  undetermined_tags: number
  undetermined_rate: number | null
  relevance: DimensionMetrics
  semantics: DimensionMetrics
  value: DimensionMetrics
}

/** judge 生产数量漏斗（审计跨片求和后整 run 算比率，None≠0）：
 * raw 原始生成 → merged 合并保留 → verdicts judge 返回 → judge_passed
 * 过滤段通过 → rank_kept 排序配额保留 → final 目录归属成功。 */
export interface JudgeFunnel {
  raw: number
  merged: number
  verdicts: number
  judge_passed: number
  cluster_kept?: number
  rank_kept: number
  final: number
  merge_keep_rate: number | null
  judge_coverage: number | null
  judge_pass_rate: number | null
  judge_pass_rate_scored: number | null
  cluster_keep_rate?: number | null
  rank_keep_rate: number | null
  dir_mount_rate: number | null
  final_keep_rate: number | null
}

/** judge 三维分跨片聚合（审计 verdicts 行）：n 有效分条数、null 无法判断数。 */
export interface JudgeScoreSummary {
  n: number
  average: number | null
  distribution: number[]
  null: number
}

/** 逐片明细行（report.json movies 行）：产出原样携带（含三维评审与 judge
 * 参考）+ 片级统计；评审失败片 pass_rate/metrics 为 null（产出不作废）。 */
export interface OpenMovieRow {
  tt: string
  title: string
  ok: boolean
  error: string | null
  review_error: string | null
  elapsed_ms: number
  n_hits: number
  n_new_words: number
  hits: OpenHitRow[]
  new_words: OpenNewWordRow[]
  pass_rate: number | null
  new_pass: number | null
  hit_pass: number | null
  review_missing: number
  review_pending: number
  metrics: QualityMetrics | null
}

/** 开放评测报告（GET /runs/{id}/report，report.json 原样透出，与后端
 * assemble_open_report 结构一致，键名逐一镜像 open_report.py，改动前先读
 * 该模块的组装代码）：banner 为 **list[str]**（部分运行/同模评审/小样本，
 * 全量干净 run → 空数组）；warnings 为口径警示列表。汇总微口径：通过率分母
 * 只计有判定行，评审失败片整体排除（None≠0）。 */
export interface OpenEvalReport {
  schema_version: number
  status: string
  source_run_id: string | null
  banner: string[]
  sample_accounting: {
    total: number
    ok: number
    failed: number
    review_failed: number
  }
  summary: {
    total_tags: number
    reviewed_tags: number
    passed_tags: number
    judged_tags: number
    review_pending: number
    hits: number
    new_words: number
    new_ratio: number | null
    pass_rate: number | null
    new_pass: number | null
    hit_pass: number | null
    review_missing: number
    metrics: QualityMetrics | null
    /** 行级 judge↔评审一致性对照（评审归一 Task 9，schema v4 新增；
     * 旧 run 缺键 → undefined）。denominator = 复算可判且 judge 参考三维
     * 齐的行数，review_rejected = 其中评审否决（judge 参考按构造全放行，
     * 评审否决即分歧）；disagreement_rate 分母 0 → null（None≠0）；
     * review_undetermined = 复算 None（轴 null）行数；no_judge_reference =
     * 复算可判但缺 judge 参考或其三维不齐的行数。axis_delta 逐维取两侧都
     * 有该维分数的行（value 用 judge_reference["value"] vs value.score）。 */
    consistency?: {
      denominator: number
      review_rejected: number
      disagreement_rate: number | null
      review_undetermined: number
      no_judge_reference: number
      axis_delta?: Partial<Record<'relevance' | 'semantics' | 'value', {
        mean_abs: number | null
        exact_rate: number | null
        n: number
      }>>
    }
  }
  produced_stats: {
    counts: { tt: string; hits: number; new_words: number }[]
    min: number | null
    median: number | null
    max: number | null
  }
  judge: {
    funnel: JudgeFunnel | null
    scores: Record<string, JudgeScoreSummary>
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
    review_config?: {
      reasoning_effort?: string | null
      max_tokens?: number | null
      max_concurrent_requests?: number
      pass_mode?: 'model' | 'threshold'
    }
    library_version: string | null
    dataset_id: string | null
    modalities: string[] | null
  }
  movies: OpenMovieRow[]
  failed_movies: { tt: string; error: string }[]
  review_failed_movies: { tt: string; review_error: string }[]
  warnings: string[]
}

/** 生产 judge 的一条判决（审计 verdicts 扁平行，键与 parse_verdicts 一致，
 * 由 pipeline._audit_verdict_rows 投影）：三维分 0-4（value 为三方向加权
 * 小数）或 null；relevance_reason/semantics_reason/value_reasons/
 * verify_fail 为评审归一（mtg-v15）扁平投影新增键（终判逐轴理由与核验
 * 失败标记）；dir_fits/in_dir 仅兼容历史审计产物。 */
export interface JudgeVerdictRow {
  tag: string
  candidate_id?: string
  relevance: number | null
  semantics: number | null
  value: number | null
  value_scores?: Record<string, number>
  tag_kind?: string
  relevance_reason?: string
  semantics_reason?: string
  value_reasons?: string
  verify_fail?: string | null
  confidence?: number
  scenes?: string[]
  quote?: string | null
  reason?: string
  in_dir?: boolean | null
  dir_fits?: Record<string, boolean>
}

/** 统一判决引擎 trace（mtg.verdicts.run_verdicts，judge/评审同键族）：
 * batches 逐批调用现场（batch 批次号、attempt 轮次、targets 本批目标、
 * raw 原始响应、error 失败原因、missing 漏判目标），errors 为最终未获判决
 * 目标的 dict（target key → 原因），salvaged 为漏判恢复条数。 */
export interface VerdictRunTrace {
  batches: {
    batch: string
    attempt: number
    targets?: string[]
    raw?: string
    error?: string | null
    missing?: string[]
  }[]
  errors?: Record<string, string>
  salvaged?: number
}

/** legacy judge trace（旧 run 审计产物）：逐次响应/错误按 attempt 索引的
 * 列表。与新键族按键存在性分流（前端读侧 `batches in trace`）。 */
export interface LegacyJudgeTrace {
  raw?: { attempt: number; content: string }[]
  errors?: { attempt: number; error: string }[]
  salvaged?: number
}

/** 生产审计（audit/<tt>.json 的诊断子集）：候选、判决、淘汰原因、最终保留。 */
export interface OpenMovieAudit {
  version?: string
  error?: string
  merged?: {
    tag: string
    candidate_id?: string
    stage?: number
    aliases?: string[]
    dimension?: string | null
    scope?: string | null
  }[]
  clustering?: {
    input: number
    target: number
    clusters: { representative: string; members: string[] }[]
  }
  candidates?: string[]
  directory_targets?: Record<string, string[]>
  verdicts?: JudgeVerdictRow[]
  dropped?: { tag: string; candidate_id?: string; why: string }[]
  accepted?: {
    tag: string; candidate_id?: string; candidate?: string; dir_id?: string | null; origin?: string
  }[]
  library?: { truncated?: { tag: string; candidate_id?: string; band?: string | null }[] }
  judge_trace?: VerdictRunTrace | LegacyJudgeTrace
}

/** 评审现场（review/<tt>.json）：逐批原始响应与失败原因。 */
export interface OpenMovieReviewTrace {
  error: string | null
  batches?: { batch: string; attempt: number; raw: string; error?: string; targets?: string[]; missing?: string[] }[]
  errors?: Record<string, string>
}

/** 单片诊断：两份现场都可能缺（produce 失败无审计 / 未评审无 review）。 */
export interface OpenMovieDiagnostics {
  tt: string
  audit: OpenMovieAudit | null
  review: OpenMovieReviewTrace | null
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

/** 单片诊断（逐片明细展开时懒加载）：生产审计 + 评审原始响应。整片产出
 * 为 0 时报告没有任何标签行，候选去向只能从这里看。两份都没有 → 404。 */
export function getOpenMovieDiagnostics(runId: string, tt: string): Promise<OpenMovieDiagnostics> {
  return apiFetch<OpenMovieDiagnostics>(`/api/v1/eval/open/runs/${runId}/movies/${tt}/diagnostics`)
}

/** 产物下载路径（白名单五件套）：纯路径拼接供 <a href>/window.open 使用，
 * 不经 apiFetch（非 JSON 响应）。 */
export function openFileUrl(runId: string, name: OpenFileName): string {
  return `/api/v1/eval/open/runs/${runId}/files/${name}`
}
