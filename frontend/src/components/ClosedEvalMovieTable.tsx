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
import { useEffect, useMemo, useState } from 'react'
import type { JSX, Key } from 'react'
import { Alert, Select, Spin, Table, Typography } from 'antd'
import type { TableProps } from 'antd'
import {
  getRunMovieDetail,
  getRunMovies,
  type MovieDetail,
  type MovieSummaryPayload,
  type MovieSummaryRow,
  type MissTag,
  type OutOfSpaceTag,
  type ProducedTag,
  type ThetaQuery,
} from '../api/eval'

const NA = '暂无'

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

/** 明细三组对照（成功片）：计分清单（GT 空间内，服务端 -sf 序，全量滚动）/
 * 空间外 / 漏检（服务端 gt 分降序）。标签名缺失回退 tag_id；GT 分缺失「暂无」；
 * 命中 ● / 未命中 —。 */
function DetailGroups({ detail }: { detail: MovieDetail }): JSX.Element {
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
  ]
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

  /** 当前生效门（选择器初值与表头字面量用）：已选取已选，否则取 payload 回传。 */
  const effectiveThetaP = thetaP ?? payload?.theta_p
  const effectiveThetaR = thetaR ?? payload?.theta_r

  /** 已选 θ 的 query 形状（未选字段不携带；全未选 → undefined 不带参）。 */
  const thetaArgs = useMemo<ThetaQuery | undefined>(() => {
    if (thetaP === null && thetaR === null) return undefined
    return { p: thetaP ?? undefined, r: thetaR ?? undefined }
  }, [thetaP, thetaR])

  // 摘要随 runId 拉取；换 run 清摘要/展开态/θ 选择（明细缓存按 runId 分键，
  // 无需清）
  useEffect(() => {
    let cancelled = false
    setPayload(null)
    setSummaryError(null)
    setExpandedKeys([])
    setThetaP(null)
    setThetaR(null)
    getRunMovies(runId)
      .then((p) => {
        if (!cancelled) setPayload(p)
      })
      .catch((e: unknown) => {
        if (!cancelled) setSummaryError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      cancelled = true
    }
  }, [runId])

  /** θ 选择器切换：重拉摘要（带参）+ 清明细缓存 + 收起展开行——θ_r 变了
   * 缓存里的 hit/漏检过期（θ_p-only 变更明细技术上仍有效，但统一清不区分，
   * 口径简单）。旧表保持可见（不闪 Spin），新 payload 到达即整体替换。 */
  function changeTheta(which: 'p' | 'r', value: number): void {
    const nextP = which === 'p' ? value : thetaP
    const nextR = which === 'r' ? value : thetaR
    setThetaP(nextP)
    setThetaR(nextR)
    setExpandedKeys([])
    setDetails(new Map())
    getRunMovies(runId, {
      p: nextP ?? undefined,
      r: nextR ?? undefined,
    })
      .then((p) => setPayload(p))
      .catch((e: unknown) =>
        setSummaryError(e instanceof Error ? e.message : String(e)))
  }

  /** 展开懒加载：成功片且无缓存条目（pending/detail/error 均算）才发请求；
   * 失败片不发（展开区只渲染 error 原文）。写回闭包持点击时的 runId 键——
   * 换 run 后落写旧键，不污染新 run；θ 已选时带参（hit/漏检跟 θ_r）。 */
  function handleExpand(expanded: boolean, record: MovieSummaryRow): void {
    setExpandedKeys((keys) =>
      expanded ? [...keys, record.tt] : keys.filter((k) => k !== record.tt),
    )
    if (!expanded || !record.ok) return
    const key = detailKey(runId, record.tt)
    if (details.has(key)) return
    setDetails((prev) => new Map(prev).set(key, 'pending'))
    // 未选 θ → 两参调用（走服务端缺省档）；已选 → 带 θ 取数（hit/漏检跟 θ_r）
    const request = thetaArgs === undefined
      ? getRunMovieDetail(runId, record.tt)
      : getRunMovieDetail(runId, record.tt, thetaArgs)
    request
      .then((d) => {
        setDetails((prev) => new Map(prev).set(key, d))
      })
      .catch((e: unknown) => {
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
    return <DetailGroups detail={cached} />
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
    </div>
  )
}
