// 闭环评测 API：/eval/closed 路由族的类型契约与取数函数（端点数以后端
// eval.py 路由模块 docstring 为准，不在此重复计数——固定数字曾随端点
// 增加而过时）。类型形状与后端 tagplatform/api/routers/eval.py 的
// Pydantic 模型、closed_eval 组装的
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

/** 历史摘要行（与后端 RunSummaryOut 一致）：摘要四键按统一取格约定
 * （P@10/nDCG@10 @θ≈0.2、R@20 @θ≈0.8 的最近有值格，custom-θ run 回退；
 * 无报告/无有值格 → null，宁缺勿崩）；error 为失败留痕（终态 failed 才有）；
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

/** 删除终态 run（磁盘产物 + 进程内注册表）→ {ok: true}；运行中（含终态
 * 落盘窄窗口）→ 409 detail 带原因；不存在/已删 → 404（重复删除即 404，
 * 与 getRun 未知 run 同口径）。经 ApiError.message 透出后端 detail。 */
export function deleteRun(runId: string): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>(`/api/v1/eval/closed/runs/${runId}`, {
    method: 'DELETE',
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

// ---------------------------------------------------------------- 单片明细（badcase 视图）

/** 漏斗十键计数（与后端 FunnelCountsOut 一致，全 number）：n_recall 召回
 * 在册、n_mentioned 给过适用度、n_kept/n_gate/n_unmountable/n_m_cap 为四类
 * 结局分流计数（unmentioned 不单设键，= n_recall − 其余四类）；n_fabricated
 * 编造条数、n_batches 批次数、parse_retries 重试批次数、max_tags 全局输出
 * 上限 M。trace 缺失（老 run）→ 整体 null「暂无」（None≠0）。 */
export interface FunnelCounts {
  n_recall: number
  n_mentioned: number
  n_kept: number
  n_gate: number
  n_unmountable: number
  n_m_cap: number
  n_fabricated: number
  n_batches: number
  parse_retries: number
  max_tags: number
}

/** 单片摘要行（与后端 MovieSummaryRowOut 一致）：指标 float | None（None=
 * 算不出——失败片/无交集/θ-空相关集，前端「暂无」，None≠0）；行序由服务
 * 层给出（失败置顶 → p_at_k 升序 → null 垫底 → 同分按 tt），前端不重排；
 * funnel 为漏斗十键计数（新 run 且 raw 行带 trace 才有；老 run/失败片 →
 * null「暂无」）。 */
export interface MovieSummaryRow {
  tt: string
  title: string
  ok: boolean
  error: string | null
  spearman: number | null
  p_at_k: number | null
  r_at_k: number | null
  elapsed_ms: number
  n_produced: number
  n_relevant: number
  funnel: FunnelCounts | null
}

/** 单片摘要包层（与后端 MovieSummaryOut 一致，增补 2026-11-22 拆 θ）：
 * theta_p/theta_r 为 P 门与 R 门实际取值（query 参数或统一取格约定的缺省
 * θ≈0.2/θ≈0.8）；thetas 为该 run 网格档（选择器选项源）；前端表头按这些键拼字面量
 * （P@{k_p}(θ{theta_p}) / R@{k_r}(θ{theta_r})）。 */
export interface MovieSummaryPayload {
  theta_p: number
  theta_r: number
  thetas: number[]
  k_p: number
  k_r: number
  rows: MovieSummaryRow[]
}

/** θ 门参数（getRunMovies/getRunMovieDetail 的可选 query）：p/r 各自独立，
 * 未选的字段不携带（服务端按缺省=最接近 0.4 取档）。 */
export interface ThetaQuery {
  p?: number
  r?: number
}

/** 计分清单条目（与后端 ProducedTagOut 一致，GT 空间内按 -sf 序）：hit =
 * gt_score 非 null 且 ≥ theta_used；tag_name 沿用产出原文（null → 前端
 * 回退 tag_id）。 */
export interface ProducedTag {
  tag_id: string
  tag_name: string | null
  sf: number
  confidence: number
  evidence_count: number
  gt_score: number | null
  hit: boolean
}

/** 空间外产出条目（与后端 OutOfSpaceTagOut 一致）：不在本批 GT 空间，
 * 不计分只列名。 */
export interface OutOfSpaceTag {
  tag_id: string
  tag_name: string | null
  sf: number
}

/** 漏检条目（与后端 MissTagOut 一致）：相关未产出，服务端按 gt 分降序；
 * tag_name 从当前标签库回填（库缺该标签 → null → 前端回退 tag_id）；
 * lost_at 为丢失环节（unrecalled/unmentioned/gate/unmountable/m_cap）或
 * null——trace 缺失（老 run）与 kept-却-漏检的不自洽防御同走 null（None≠0，
 * 没数据≠没召回），前端做中文映射。 */
export interface MissTag {
  tag_id: string
  tag_name: string | null
  gt_score: number
  lost_at: string | null
}

/** 召回候选踪迹行（与后端 TraceCandidateOut 一致，服务端序=召回序）：
 * 身份/目录/召回分/批次序入册定死；结局三字段 resolve 回填——unmentioned
 * 候选的 applicability/sf 双 null（「未提及」无显式上报点）、gate 候选 sf
 * null（门控丢弃点不算终分）。outcome 五值枚举 kept|unmentioned|gate|
 * unmountable|m_cap，前端做中文映射。 */
export interface TraceCandidate {
  tag_id: string
  tag_name: string | null
  dir_id: string
  recall: number | null
  batch: number | null
  outcome: string
  applicability: number | null
  sf: number | null
}

/** 批次追踪行（与后端 TraceBatchOut 一致）：parse_retried=首败重试成功；
 * 原始输出不在此行（只存 llm_raw 文件、走 movieRawUrl 下载，不进 API 载荷）。 */
export interface TraceBatchEvent {
  dir_id: string
  batch: number
  n: number
  parse_retried: boolean
}

/** 单片管线追踪包层（与后端 MovieTraceOut 一致）：counts=漏斗十键（与摘要
 * 行 funnel 同数）；candidates 服务端序=召回序；batches 批次序；fabricated
 * 按判定出现次序累积不去重；max_tags 与 counts.max_tags 同值冗余透出（服务
 * 层组装形状如此）。老 run/失败片 → 整体 null「暂无」（None≠0）。 */
export interface MovieTrace {
  counts: FunnelCounts
  candidates: TraceCandidate[]
  batches: TraceBatchEvent[]
  fabricated: string[]
  max_tags: number
}

/** 单片明细（与后端 MovieDetailOut 一致）：produced/out_of_space/misses 三
 * 表；失败片三表全空、error 原样（前端不为其发明细请求）；theta_p/theta_r
 * 与摘要包层同源（明细侧 hit/漏检分类跟 theta_r）；trace 为管线追踪包层
 * （成功片且 raw 行带 trace 才组装）或 null——null 一律「暂无」。 */
export interface MovieDetail {
  tt: string
  title: string
  ok: boolean
  error: string | null
  elapsed_ms: number
  theta_p: number
  theta_r: number
  k_p: number
  k_r: number
  produced: ProducedTag[]
  out_of_space: OutOfSpaceTag[]
  misses: MissTag[]
  trace: MovieTrace | null
}

/** θ query 拼接（增补 2026-11-22）：未传/空对象 → 无 query（服务端缺省）。 */
function thetaQuery(theta?: ThetaQuery): string {
  if (!theta) return ''
  const params = new URLSearchParams()
  if (theta.p !== undefined) params.set('theta_p', String(theta.p))
  if (theta.r !== undefined) params.set('theta_r', String(theta.r))
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}

/** run 的逐片摘要（badcase 视图主表，读盘现算）：无单片明细（运行中/产物
 * 缺失）→ 404，detail 经 ApiError.message 透出；theta 拆门可选（非法档
 * 400 带合法值清单）。 */
export function getRunMovies(
  runId: string,
  theta?: ThetaQuery,
): Promise<MovieSummaryPayload> {
  return apiFetch<MovieSummaryPayload>(
    `/api/v1/eval/closed/runs/${runId}/movies${thetaQuery(theta)}`)
}

/** 单片明细（摘要行展开懒加载）：未知 tt → 404（detail 经 ApiError.message
 * 透出）；theta 同摘要端点（明细 hit/漏检分类跟 theta_r）。 */
export function getRunMovieDetail(
  runId: string,
  tt: string,
  theta?: ThetaQuery,
): Promise<MovieDetail> {
  return apiFetch<MovieDetail>(
    `/api/v1/eval/closed/runs/${runId}/movies/${tt}${thetaQuery(theta)}`)
}

/** 单片 LLM 原始输出下载路径（llm_raw/{tt}.json，FileResponse 直下）：纯路径
 * 拼接同 fileUrl 模式，不经 apiFetch（非 JSON 响应）。但 404 会开浏览器错误
 * 页——调用方走 fetch+blob（同源请求自带会话 cookie）而非 <a> 直链。 */
export function movieRawUrl(runId: string, tt: string): string {
  return `/api/v1/eval/closed/runs/${runId}/movies/${tt}/llm-raw`
}
