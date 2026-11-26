// 闭环评测单片明细表（badcase 视图）：run 的逐片摘要主表 + 行展开懒加载单片
// 明细。摘要由服务端排好序（失败置顶 → P@K 升序 → null 垫底 → 同分按 tt），
// 前端按数组序渲染不重排，数值列仅提供 sorter 供用户点选（不预激活）；表头
// P@K/R@K 由服务端回传的档位键拼字面量、带门（P@{k_p}(θ{θ_p}) 式）。
// null 一律「暂无」（None≠0 纪律：失败片/无交集/θ-空相关集是算不出，不是 0）。
// 明细按 `runId:tt` 键懒加载并缓存（Map<key, 'pending' | MovieDetail | Error>）：
// pending 挡住同键重复请求（在途窗口内收起再展开不重发）；键带 runId 命名
// 空间——换 run 后旧 run 的在途/缓存条目不可达，防跨 run 串写（在途 promise
// 落写只会命中旧键，新 run 展开必重新请求），清缓存逻辑因此无需存在；
// 失败片不发请求、展开只渲染 error 原文（后端对其三表全空）；明细请求失败
// 在展开区透出 error.message（ApiError.message 即后端 detail 文案），失败
// 结果同样入缓存（重复展开不反复打 404）。
// θ 可调（增补 2026-11-22）：θ_P/θ_R 两个选择器（选项 = payload.thetas），
// 未选（null）= 服务端缺省档（请求不带参）；切换即重拉摘要（带参）+ 清空
// 明细缓存 + 收起展开行（θ_r 变了缓存里的 hit/漏检过期；统一清不区分 p/r）；
// 换 run 重置回未选。明细取数在 θ 已选时带参（hit/漏检跟 θ_r）。
// 代际守卫（终审修复）：genRef 在换 run 与每次切 θ 时自增——旧代际的摘要/
// 明细响应一律作废不写回（防：θ 切换后在途旧 θ 明细落入已清缓存、连续切 θ
// 的慢响应覆盖快响应、跨 run 慢响应覆盖新 run）。θ 重拉失败不毁视图：旧表
// 保留 + 内联警示（refetchError），成功即清除——恢复路径不消失。
// 管线追踪（增补 2026-11-26）：主表加「漏斗」列（召回→提及→产出，title
// 浮全链十键计数，null「暂无」）；明细区 trace 非 null 渲染漏斗条（五阶段
// 主链 mini BarChart 复用 FunnelBars + 分流/次要计数）、候选踪迹表（结局
// Select 筛选为本地受控状态——
// 不进缓存/请求逻辑，服务端召回序不重排）、LLM 原始输出下载（fetch+blob，
// 404/失败 message.error 透出不崩视图——不用 <a> 直链，404 会开浏览器错误
// 页）；trace null（老 run/失败片）渲染说明文案。misses 表加「丢失环节」列。
// 结局/丢失环节后端原样透传，中文映射集中在本文件常量。θ 门/代际守卫/
// 懒加载缓存零改动：trace 随 detail 一次拉回，不新增请求。
// 整体漏斗块（增补 2026-11-26）：主表上方全 run 聚合——payload.rows 纯派生
// （十键逐键加总 funnel 非 null 行，无任何非 null 行 → 「暂无」说明文案，
// 同 detail-trace-absent 口径）：五 Statistic 主链（门控过/挂载成差式派生
// 同 FunnelBar）+ BarChart 五行条形（显式 max=召回）+ 次要计数行（不含输出
// 上限——聚合的 max_tags 无意义）+ 每片存活率（n_kept/n_recall，召回 0 的
// 片跳过、偶数中位取中间两数均值）+ 覆盖标注（N 部有解剖数据 / 共 M 部）；
// 不新增请求、θ 门/缓存逻辑零改动。
import { useEffect, useMemo, useRef, useState } from 'react'
import type { JSX, Key } from 'react'
import { Alert, Button, Select, Spin, Statistic, Table, Typography, message } from 'antd'
import type { TableProps } from 'antd'
import BarChart from './BarChart'
import {
  getRunMovieDetail,
  getRunMovies,
  movieRawUrl,
  type FunnelCounts,
  type MovieDetail,
  type MovieSummaryPayload,
  type MovieSummaryRow,
  type MissTag,
  type MovieTrace,
  type OutOfSpaceTag,
  type ProducedTag,
  type ThetaQuery,
  type TraceCandidate,
} from '../api/eval'

const NA = '暂无'

/** 结局/丢失环节中文映射（outcome/lost_at 后端原样透传，前端统一映射；
 * kept 同时用于踪迹结局；unrecalled 仅出现在 misses.lost_at）。 */
const OUTCOME_LABELS: Record<string, string> = {
  kept: '保留',
  unmentioned: '未提及',
  gate: '门控丢弃',
  unmountable: '挂载失败',
  m_cap: '上限截断',
  unrecalled: '未召回',
}

/** 踪迹结局五值（筛选选项序=展示序；unrecalled 是丢失环节不是结局，不进）。 */
const OUTCOME_VALUES = ['kept', 'unmentioned', 'gate', 'unmountable', 'm_cap'] as const

/** 结局筛选选项（全部 + 五结局）。 */
const OUTCOME_FILTER_OPTIONS = [
  { value: 'all', label: '全部' },
  ...OUTCOME_VALUES.map((v) => ({ value: v, label: OUTCOME_LABELS[v] })),
]

/** 结局/丢失环节中文标签（未知值防御回退原文）。 */
function outcomeLabel(outcome: string): string {
  return OUTCOME_LABELS[outcome] ?? outcome
}

/** 漏斗全链计数浮窗文案（主表漏斗列 title；十键全量）。 */
function funnelTitle(f: FunnelCounts): string {
  return (
    `召回 ${f.n_recall} · 提及 ${f.n_mentioned} · 产出 ${f.n_kept} · `
    + `门控丢弃 ${f.n_gate} · 挂载失败 ${f.n_unmountable} · 上限截断 ${f.n_m_cap} · `
    + `编造 ${f.n_fabricated} · 批次 ${f.n_batches}（重试 ${f.parse_retries}） · `
    + `输出上限 ${f.max_tags}`
  )
}

/** null → 「暂无」（None≠0，与 ClosedEvalReport 同口径）。 */
function na(v: number | null): string {
  return v === null ? NA : String(v)
}

/** 可空指标列比较器：null 一律垫底（与服务端排序同口径）。 */
function nullsLast(
  field: 'spearman' | 'p_at_k' | 'r_at_k',
): (a: MovieSummaryRow, b: MovieSummaryRow) => number {
  return (a, b) => {
    if (a[field] === null && b[field] === null) return 0
    if (a[field] === null) return 1
    if (b[field] === null) return -1
    return (a[field] as number) - (b[field] as number)
  }
}

/** 非空数值列比较器（产出数/相关数/耗时）。 */
function byNumber(
  field: 'n_produced' | 'n_relevant' | 'elapsed_ms',
): (a: MovieSummaryRow, b: MovieSummaryRow) => number {
  return (a, b) => a[field] - b[field]
}

// ---------- 整体漏斗块（主表上方全 run 聚合，增补 2026-11-26） ----------

/** 全 run 漏斗聚合（纯函数）：十键逐键加总 funnel 非 null 行（失败片/老 run
 * 行 null 不计入），n_movies 为非 null 行数；无任何非 null 行 → null——
 * 老 run 是「算不出」不是全 0（None≠0），渲染层走「暂无」说明文案。 */
export function aggregateFunnel(
  rows: MovieSummaryRow[],
): { counts: FunnelCounts; n_movies: number } | null {
  const counts: FunnelCounts = {
    n_recall: 0,
    n_mentioned: 0,
    n_kept: 0,
    n_gate: 0,
    n_unmountable: 0,
    n_m_cap: 0,
    n_fabricated: 0,
    n_batches: 0,
    parse_retries: 0,
    max_tags: 0,
  }
  let nMovies = 0
  for (const row of rows) {
    const f = row.funnel
    if (f === null) continue
    nMovies += 1
    counts.n_recall += f.n_recall
    counts.n_mentioned += f.n_mentioned
    counts.n_kept += f.n_kept
    counts.n_gate += f.n_gate
    counts.n_unmountable += f.n_unmountable
    counts.n_m_cap += f.n_m_cap
    counts.n_fabricated += f.n_fabricated
    counts.n_batches += f.n_batches
    counts.parse_retries += f.parse_retries
    counts.max_tags += f.max_tags
  }
  return nMovies === 0 ? null : { counts, n_movies: nMovies }
}

/** 每片存活率统计（纯函数）：逐片 n_kept/n_recall（funnel null 或召回 0 的
 * 片跳过——0/0 不可算，None≠0 不造 0）；升序取 min/中位/max，偶数个取中间
 * 两数均值；无 eligible 片 → null（「暂无」）。 */
export function survivalStats(
  rows: MovieSummaryRow[],
): { min: number; median: number; max: number } | null {
  const rates: number[] = []
  for (const row of rows) {
    if (row.funnel === null || row.funnel.n_recall === 0) continue
    rates.push(row.funnel.n_kept / row.funnel.n_recall)
  }
  if (rates.length === 0) return null
  const sorted = [...rates].sort((a, b) => a - b)
  const mid = Math.floor(sorted.length / 2)
  const median = sorted.length % 2 === 1
    ? sorted[mid]
    : (sorted[mid - 1] + sorted[mid]) / 2
  return { min: sorted[0], median, max: sorted[sorted.length - 1] }
}

/** 整体漏斗条形图（RunFunnelSummary 内）：主链五阶段同 FunnelBar 差式口径
 * （门控过 = 提及−门控丢弃、挂载成 = 门控过−挂载失败），显式 max=召回——
 * 各阶段分母同为召回池，行间可比；复用既有 BarChart 纯 CSS 条形图。 */
export function FunnelBars({ counts }: { counts: FunnelCounts }): JSX.Element {
  const passedGate = counts.n_mentioned - counts.n_gate
  const mountedOk = passedGate - counts.n_unmountable
  return (
    <BarChart
      items={[
        { label: '召回', value: counts.n_recall, max: counts.n_recall },
        { label: '提及', value: counts.n_mentioned, max: counts.n_recall },
        { label: '门控过', value: passedGate, max: counts.n_recall },
        { label: '挂载成', value: mountedOk, max: counts.n_recall },
        { label: '产出', value: counts.n_kept, max: counts.n_recall },
      ]}
    />
  )
}

/** 整体漏斗块（主 return θ 控件与主表之间）：payload.rows 纯派生渲染，不
 * 新增请求。aggregateFunnel null（老 run）→ 说明文案；否则五 Statistic 主链
 * （聚合值与差式派生精确相等）+ FunnelBars + 次要计数行（门控丢弃/挂载失败/
 * 上限截断/编造/批次（解析重试）——不含输出上限：聚合的 max_tags 无意义）+
 * 每片存活率行（null → 「暂无」）+ 覆盖标注（N 部有解剖数据 / 共 M 部）。 */
export function RunFunnelSummary({ rows }: { rows: MovieSummaryRow[] }): JSX.Element {
  const agg = aggregateFunnel(rows)
  if (agg === null) {
    return (
      <div data-testid="run-funnel-absent">
        <Typography.Text type="secondary">
          本 run 早于管线追踪功能，无解剖数据
        </Typography.Text>
      </div>
    )
  }
  const passedGate = agg.counts.n_mentioned - agg.counts.n_gate
  const mountedOk = passedGate - agg.counts.n_unmountable
  const survival = survivalStats(rows)
  return (
    <div data-testid="run-funnel">
      <Typography.Title level={5}>整体漏斗</Typography.Title>
      <div style={{ display: 'flex', gap: 48, flexWrap: 'wrap' }}>
        <div data-testid="run-funnel-recall">
          <Statistic title="召回" value={agg.counts.n_recall} />
        </div>
        <div data-testid="run-funnel-mentioned">
          <Statistic title="提及" value={agg.counts.n_mentioned} />
        </div>
        <div data-testid="run-funnel-passed">
          <Statistic title="门控过" value={passedGate} />
        </div>
        <div data-testid="run-funnel-mounted">
          <Statistic title="挂载成" value={mountedOk} />
        </div>
        <div data-testid="run-funnel-kept">
          <Statistic title="产出" value={agg.counts.n_kept} />
        </div>
      </div>
      <FunnelBars counts={agg.counts} />
      <div>
        <Typography.Text type="secondary" data-testid="run-funnel-secondary">
          {`门控丢弃 ${agg.counts.n_gate} · 挂载失败 ${agg.counts.n_unmountable} · `
            + `上限截断 ${agg.counts.n_m_cap} · 编造 ${agg.counts.n_fabricated} · `
            + `批次 ${agg.counts.n_batches}（解析重试 ${agg.counts.parse_retries}）`}
        </Typography.Text>
      </div>
      <div>
        <Typography.Text type="secondary" data-testid="run-funnel-survival">
          {survival === null
            ? `每片存活率 ${NA}`
            : `每片存活率 min ${survival.min} · 中位 ${survival.median} · max ${survival.max}`}
        </Typography.Text>
      </div>
      <div>
        <Typography.Text type="secondary" data-testid="run-funnel-coverage">
          {`合计覆盖 ${agg.n_movies} 部 / 共 ${rows.length} 部`}
        </Typography.Text>
      </div>
    </div>
  )
}

/** 漏斗条（trace 非 null 的明细区首块）：主链五阶段 复用 FunnelBars 迷你
 * BarChart（差式派生随组件内聚——门控过/挂载成由分流键差出，提及池按结局
 * 分流完备：后端 FunnelCountsOut 注释口径——kept/gate/unmountable/m_cap 全带
 * 适用度，unmentioned 双 null 不入池；显式 max=召回行间可比）；次要计数列
 * 分流四键/编造/批次（含解析重试）/输出上限——单片 M 有意义，与整体块不含
 * 输出上限的区分是刻意口径。 */
function FunnelBar({ counts }: { counts: FunnelCounts }): JSX.Element {
  return (
    <div data-testid="detail-funnel">
      <Typography.Title level={5}>管线漏斗</Typography.Title>
      <FunnelBars counts={counts} />
      <Typography.Text type="secondary" style={{ marginLeft: 12 }}>
        {`门控丢弃 ${counts.n_gate} · 挂载失败 ${counts.n_unmountable} · `
          + `上限截断 ${counts.n_m_cap} · 编造 ${counts.n_fabricated} · `
          + `批次 ${counts.n_batches}（解析重试 ${counts.parse_retries}） · `
          + `输出上限 ${counts.max_tags}`}
      </Typography.Text>
    </div>
  )
}

/** 候选踪迹表（trace 非 null 的明细区第二块）：列 标签/召回分/目录/批次/
 * A/Sf/结局；结局 Select 筛选为本地受控状态（不进展开缓存/请求逻辑，展开
 * 收起不触碰）；服务端序=召回序不重排不排序；结局列中文映射，null 数值
 * 「暂无」。 */
function TraceSection({ trace }: { trace: MovieTrace }): JSX.Element {
  const [outcome, setOutcome] = useState<string>('all')
  const rows = outcome === 'all'
    ? trace.candidates
    : trace.candidates.filter((c) => c.outcome === outcome)
  const columns: TableProps<TraceCandidate>['columns'] = [
    { title: '标签', key: 'tag', render: (_, c) => c.tag_name ?? c.tag_id },
    { title: '召回分', dataIndex: 'recall', render: na },
    { title: '目录', dataIndex: 'dir_id' },
    { title: '批次', dataIndex: 'batch', render: na },
    { title: 'A', dataIndex: 'applicability', render: na },
    { title: 'Sf', dataIndex: 'sf', render: na },
    { title: '结局', key: 'outcome', render: (_, c) => outcomeLabel(c.outcome) },
  ]
  return (
    <div data-testid="detail-trace">
      <Typography.Title level={5}>候选踪迹</Typography.Title>
      <div style={{ marginBottom: 8 }}>
        <Select
          data-testid="trace-outcome-select"
          size="small"
          style={{ width: 140 }}
          value={outcome}
          options={OUTCOME_FILTER_OPTIONS}
          virtual={false}
          onChange={(v) => setOutcome(v)}
        />
      </div>
      <Table<TraceCandidate>
        rowKey="tag_id"
        columns={columns}
        dataSource={rows}
        pagination={false}
        size="small"
        scroll={{ y: 240 }}
      />
    </div>
  )
}

/** 明细三组对照（成功片）：计分清单（GT 空间内，服务端 -sf 序，全量滚动）/
 * 空间外 / 漏检（服务端 gt 分降序，增「丢失环节」交叉列）。标签名缺失回退
 * tag_id；GT 分缺失「暂无」；命中 ● / 未命中 —。trace 非 null 追加漏斗条/
 * 候选踪迹/原始输出下载；null（老 run/失败片）渲染说明文案（显式优于神秘
 * 消失），且下载不渲染（后端三重守卫 404，直链会开浏览器错误页）。 */
function DetailGroups({ detail, runId }: { detail: MovieDetail; runId: string }): JSX.Element {
  const producedColumns: TableProps<ProducedTag>['columns'] = [
    { title: '标签', key: 'tag', render: (_, t) => t.tag_name ?? t.tag_id },
    { title: 'sf', dataIndex: 'sf', render: na },
    { title: 'conf', dataIndex: 'confidence', render: na },
    { title: 'GT 分', dataIndex: 'gt_score', render: na },
    { title: '命中', dataIndex: 'hit', render: (hit: boolean) => (hit ? '●' : '—') },
  ]
  const outOfSpaceColumns: TableProps<OutOfSpaceTag>['columns'] = [
    { title: '标签', key: 'tag', render: (_, t) => t.tag_name ?? t.tag_id },
    { title: 'sf', dataIndex: 'sf', render: na },
  ]
  const missColumns: TableProps<MissTag>['columns'] = [
    { title: '标签', key: 'tag', render: (_, t) => t.tag_name ?? t.tag_id },
    { title: 'GT 分', dataIndex: 'gt_score', render: na },
    {
      title: '丢失环节',
      dataIndex: 'lost_at',
      render: (lost: string | null) => (lost === null ? NA : outcomeLabel(lost)),
    },
  ]

  /** LLM 原始输出下载：fetch+blob（同源请求自带会话 cookie）触发浏览器
   * 下载；不用 <a> 直链——404 会开浏览器错误页。失败 message.error 透出
   * （优先后端 detail 文案，非 JSON 体回退 HTTP 状态码；网络异常透出
   * message），视图不崩。 */
  async function downloadRaw(): Promise<void> {
    try {
      const resp = await fetch(movieRawUrl(runId, detail.tt))
      if (!resp.ok) {
        let reason = `HTTP ${resp.status}`
        try {
          const body = (await resp.json()) as { detail?: unknown }
          if (typeof body?.detail === 'string') reason = body.detail
        } catch {
          // 非 JSON 体（代理错误页等）→ 保留 HTTP 状态码
        }
        message.error(`原始输出下载失败：${reason}`)
        return
      }
      const blob = await resp.blob()
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `${detail.tt}.json`
      anchor.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div data-testid="detail-produced">
        <Typography.Title level={5}>计分清单</Typography.Title>
        <Table<ProducedTag>
          rowKey="tag_id"
          columns={producedColumns}
          dataSource={detail.produced}
          pagination={false}
          size="small"
          scroll={{ y: 240 }}
        />
      </div>
      <div data-testid="detail-out-of-space">
        <Typography.Title level={5}>空间外</Typography.Title>
        <Table<OutOfSpaceTag>
          rowKey="tag_id"
          columns={outOfSpaceColumns}
          dataSource={detail.out_of_space}
          pagination={false}
          size="small"
        />
      </div>
      <div data-testid="detail-misses">
        <Typography.Title level={5}>漏检</Typography.Title>
        <Table<MissTag>
          rowKey="tag_id"
          columns={missColumns}
          dataSource={detail.misses}
          pagination={false}
          size="small"
        />
      </div>
      {detail.trace === null ? (
        <div data-testid="detail-trace-absent">
          <Typography.Text type="secondary">
            本 run 早于管线追踪功能，无解剖数据
          </Typography.Text>
        </div>
      ) : (
        <>
          <FunnelBar counts={detail.trace.counts} />
          <TraceSection trace={detail.trace} />
          <div>
            <Button
              size="small"
              data-testid="llm-raw-download"
              onClick={() => {
                void downloadRaw()
              }}
            >
              下载 LLM 原始输出
            </Button>
          </div>
        </>
      )}
    </div>
  )
}

/** 明细缓存键：runId 命名空间（换 run 后旧 run 条目不可达，防跨 run 串写）。 */
function detailKey(runId: string, tt: string): string {
  return `${runId}:${tt}`
}

export default function ClosedEvalMovieTable({ runId }: { runId: string }): JSX.Element {
  const [payload, setPayload] = useState<MovieSummaryPayload | null>(null)
  const [summaryError, setSummaryError] = useState<string | null>(null)
  const [expandedKeys, setExpandedKeys] = useState<Key[]>([])
  const [details, setDetails] = useState<Map<string, 'pending' | MovieDetail | Error>>(
    new Map(),
  )
  // θ 门（null = 未选 → 用服务端缺省档、请求不带参；换 run 重置）
  const [thetaP, setThetaP] = useState<number | null>(null)
  const [thetaR, setThetaR] = useState<number | null>(null)
  // θ 重拉失败的内联警示（不毁视图，成功即清除）
  const [refetchError, setRefetchError] = useState<string | null>(null)
  // 请求代际：换 run / 切 θ 自增——旧代际响应一律作废不写回
  const genRef = useRef(0)

  /** 当前生效门（选择器初值与表头字面量用）：已选取已选，否则取 payload 回传。 */
  const effectiveThetaP = thetaP ?? payload?.theta_p
  const effectiveThetaR = thetaR ?? payload?.theta_r

  /** 已选 θ 的 query 形状（未选字段不携带；全未选 → undefined 不带参）。 */
  const thetaArgs = useMemo<ThetaQuery | undefined>(() => {
    if (thetaP === null && thetaR === null) return undefined
    return { p: thetaP ?? undefined, r: thetaR ?? undefined }
  }, [thetaP, thetaR])

  // 摘要随 runId 拉取；换 run 清摘要/展开态/θ 选择/警示（明细缓存按
  // runId 分键，无需清），并使旧 run 的在途响应全部作废
  useEffect(() => {
    genRef.current += 1
    const gen = genRef.current
    let cancelled = false
    setPayload(null)
    setSummaryError(null)
    setExpandedKeys([])
    setThetaP(null)
    setThetaR(null)
    setRefetchError(null)
    getRunMovies(runId)
      .then((p) => {
        if (!cancelled && gen === genRef.current) setPayload(p)
      })
      .catch((e: unknown) => {
        if (!cancelled && gen === genRef.current) {
          setSummaryError(e instanceof Error ? e.message : String(e))
        }
      })
    return () => {
      cancelled = true
    }
  }, [runId])

  /** θ 选择器切换：重拉摘要（带参）+ 清明细缓存 + 收起展开行——θ_r 变了
   * 缓存里的 hit/漏检过期（θ_p-only 变更明细技术上仍有效，但统一清不区分，
   * 口径简单）。旧表保持可见（不闪 Spin），新 payload 到达即整体替换；
   * 失败不毁视图——内联警示、表格照旧。代际自增使在途旧响应（慢摘要/
   * 旧 θ 明细）一律作废。 */
  function changeTheta(which: 'p' | 'r', value: number): void {
    genRef.current += 1
    const gen = genRef.current
    const nextP = which === 'p' ? value : thetaP
    const nextR = which === 'r' ? value : thetaR
    setThetaP(nextP)
    setThetaR(nextR)
    setExpandedKeys([])
    setDetails(new Map())
    setRefetchError(null)
    getRunMovies(runId, {
      p: nextP ?? undefined,
      r: nextR ?? undefined,
    })
      .then((p) => {
        if (gen !== genRef.current) return
        setPayload(p)
        setRefetchError(null)
      })
      .catch((e: unknown) => {
        if (gen !== genRef.current) return
        setRefetchError(e instanceof Error ? e.message : String(e))
      })
  }

  /** 展开懒加载：成功片且无缓存条目（pending/detail/error 均算）才发请求；
   * 失败片不发（展开区只渲染 error 原文）。写回闭包持点击时的 runId 键与
   * 请求代际——换 run/切 θ 后旧代际落写作废，不污染新视图；θ 已选时带参
   * （hit/漏检跟 θ_r）。 */
  function handleExpand(expanded: boolean, record: MovieSummaryRow): void {
    setExpandedKeys((keys) =>
      expanded ? [...keys, record.tt] : keys.filter((k) => k !== record.tt),
    )
    if (!expanded || !record.ok) return
    const key = detailKey(runId, record.tt)
    if (details.has(key)) return
    setDetails((prev) => new Map(prev).set(key, 'pending'))
    const gen = genRef.current
    // 未选 θ → 两参调用（走服务端缺省档）；已选 → 带 θ 取数（hit/漏检跟 θ_r）
    const request = thetaArgs === undefined
      ? getRunMovieDetail(runId, record.tt)
      : getRunMovieDetail(runId, record.tt, thetaArgs)
    request
      .then((d) => {
        if (gen !== genRef.current) return
        setDetails((prev) => new Map(prev).set(key, d))
      })
      .catch((e: unknown) => {
        if (gen !== genRef.current) return
        setDetails((prev) =>
          new Map(prev).set(key, e instanceof Error ? e : new Error(String(e))),
        )
      })
  }

  function renderDetail(record: MovieSummaryRow): JSX.Element {
    if (!record.ok) {
      // 失败片：不发请求，只渲染 error 原文
      return <Typography.Text type="danger">{record.error}</Typography.Text>
    }
    const cached = details.get(detailKey(runId, record.tt))
    if (cached === undefined || cached === 'pending') return <Spin size="small" />
    if (cached instanceof Error) {
      return <Alert type="error" showIcon message={cached.message} />
    }
    return <DetailGroups detail={cached} runId={runId} />
  }

  const columns = useMemo<TableProps<MovieSummaryRow>['columns']>(() => {
    if (payload === null) return []
    return [
      { title: 'tt', dataIndex: 'tt' },
      { title: '标题', dataIndex: 'title' },
      { title: '状态', dataIndex: 'ok', render: (ok: boolean) => (ok ? '成功' : '失败') },
      { title: 'Spearman', dataIndex: 'spearman', render: na, sorter: nullsLast('spearman') },
      {
        title: `P@${payload.k_p}(θ${effectiveThetaP})`,
        dataIndex: 'p_at_k',
        render: na,
        sorter: nullsLast('p_at_k'),
      },
      {
        title: `R@${payload.k_r}(θ${effectiveThetaR})`,
        dataIndex: 'r_at_k',
        render: na,
        sorter: nullsLast('r_at_k'),
      },
      {
        title: '耗时(秒)',
        key: 'elapsed_seconds',
        render: (_, r) => String(Math.round(r.elapsed_ms / 1000)),
        sorter: byNumber('elapsed_ms'),
      },
      { title: '产出数', dataIndex: 'n_produced', sorter: byNumber('n_produced') },
      { title: '相关数', dataIndex: 'n_relevant', sorter: byNumber('n_relevant') },
      {
        title: '漏斗',
        key: 'funnel',
        render: (_, r) =>
          r.funnel === null ? (
            NA
          ) : (
            <span title={funnelTitle(r.funnel)}>
              {`召回 ${r.funnel.n_recall} → 提及 ${r.funnel.n_mentioned}`
                + ` → 产出 ${r.funnel.n_kept}`}
            </span>
          ),
      },
    ]
  }, [payload, effectiveThetaP, effectiveThetaR])

  if (summaryError !== null) {
    return <Alert type="error" showIcon message={summaryError} />
  }
  if (payload === null) {
    return <Spin />
  }
  const thetaOptions = payload.thetas.map((t) => ({ value: t, label: String(t) }))
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div
        style={{ display: 'flex', gap: 8, alignItems: 'center' }}
        data-testid="theta-controls"
      >
        <Typography.Text type="secondary">θ 门：</Typography.Text>
        <span>P</span>
        <Select
          data-testid="theta-p-select"
          size="small"
          style={{ width: 90 }}
          value={effectiveThetaP}
          options={thetaOptions}
          virtual={false}
          onChange={(v) => changeTheta('p', v)}
        />
        <span>R</span>
        <Select
          data-testid="theta-r-select"
          size="small"
          style={{ width: 90 }}
          value={effectiveThetaR}
          options={thetaOptions}
          virtual={false}
          onChange={(v) => changeTheta('r', v)}
        />
      </div>
      <RunFunnelSummary rows={payload.rows} />
      <Table<MovieSummaryRow>
        rowKey="tt"
        columns={columns}
        dataSource={payload.rows}
        pagination={false}
        size="small"
        expandable={{
          expandedRowKeys: expandedKeys,
          onExpand: handleExpand,
          expandedRowRender: renderDetail,
        }}
      />
      {refetchError !== null && (
        <Alert
          type="warning"
          showIcon
          data-testid="theta-refetch-error"
          message={`θ 重拉失败，当前显示旧门数据：${refetchError}`}
        />
      )}
    </div>
  )
}
