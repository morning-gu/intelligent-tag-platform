// 闭环评测单片明细表（badcase 视图）：run 的逐片摘要主表 + 行展开懒加载单片
// 明细。摘要由服务端排好序（失败置顶 → P@K 升序 → null 垫底 → 同分按 tt），
// 前端按数组序渲染不重排，数值列仅提供 sorter 供用户点选（不预激活）；表头
// P@K/R@K 由服务端回传的档位键（k_p/k_r）拼字面量。null 一律「暂无」
// （None≠0 纪律：失败片/无交集/θ-空相关集是算不出，不是 0）。
// 明细按 tt 懒加载并缓存（Map<tt, MovieDetail | Error>）：收起再展开不重复
// 请求；失败片不发请求、展开只渲染 error 原文（后端对其三表全空）；明细
// 请求失败在展开区透出 error.message（ApiError.message 即后端 detail 文案），
// 失败结果同样入缓存（重复展开不反复打 404）。
import { useEffect, useMemo, useState } from 'react'
import type { JSX, Key } from 'react'
import { Alert, Spin, Table, Typography } from 'antd'
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

export default function ClosedEvalMovieTable({ runId }: { runId: string }): JSX.Element {
  const [payload, setPayload] = useState<MovieSummaryPayload | null>(null)
  const [summaryError, setSummaryError] = useState<string | null>(null)
  const [expandedKeys, setExpandedKeys] = useState<Key[]>([])
  const [details, setDetails] = useState<Map<string, MovieDetail | Error>>(new Map())

  // 摘要随 runId 拉取；换 run 清展开态与明细缓存（明细键 tt 属于该 run）
  useEffect(() => {
    let cancelled = false
    setPayload(null)
    setSummaryError(null)
    setExpandedKeys([])
    setDetails(new Map())
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

  /** 展开懒加载：成功片且未缓存才发请求；失败片不发（展开区只渲染 error 原文）。 */
  function handleExpand(expanded: boolean, record: MovieSummaryRow): void {
    setExpandedKeys((keys) =>
      expanded ? [...keys, record.tt] : keys.filter((k) => k !== record.tt),
    )
    if (!expanded || !record.ok || details.has(record.tt)) return
    getRunMovieDetail(runId, record.tt)
      .then((d) => {
        setDetails((prev) => new Map(prev).set(record.tt, d))
      })
      .catch((e: unknown) => {
        setDetails((prev) =>
          new Map(prev).set(
            record.tt,
            e instanceof Error ? e : new Error(String(e)),
          ),
        )
      })
  }

  function renderDetail(record: MovieSummaryRow): JSX.Element {
    if (!record.ok) {
      // 失败片：不发请求，只渲染 error 原文
      return <Typography.Text type="danger">{record.error}</Typography.Text>
    }
    const cached = details.get(record.tt)
    if (cached === undefined) return <Spin size="small" />
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
      { title: `P@${payload.k_p}`, dataIndex: 'p_at_k', render: na, sorter: nullsLast('p_at_k') },
      { title: `R@${payload.k_r}`, dataIndex: 'r_at_k', render: na, sorter: nullsLast('r_at_k') },
      {
        title: '耗时(秒)',
        key: 'elapsed_seconds',
        render: (_, r) => String(Math.round(r.elapsed_ms / 1000)),
        sorter: byNumber('elapsed_ms'),
      },
      { title: '产出数', dataIndex: 'n_produced', sorter: byNumber('n_produced') },
      { title: '相关数', dataIndex: 'n_relevant', sorter: byNumber('n_relevant') },
    ]
  }, [payload])

  if (summaryError !== null) {
    return <Alert type="error" showIcon message={summaryError} />
  }
  if (payload === null) {
    return <Spin />
  }
  return (
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
  )
}
