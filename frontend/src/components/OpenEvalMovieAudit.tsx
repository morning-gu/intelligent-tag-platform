// 单片生产审计面板：把 audit/<tt>.json 的候选去向摊开——每条合并候选的
// judge 三维分、目录契合、理由与摘录，以及它最终是保留还是因何淘汰。
// 整片产出为 0 时报告里没有任何标签行，这块是唯一能看到「11 条候选都因
// value=null 被淘汰」的地方；评审失败时同时列出逐批失败原因。
// 懒加载：只在用户展开时挂载并取数（父级折叠面板默认不渲染 children）。
import { Alert, Descriptions, Space, Spin, Table, Tag, Typography } from 'antd'
import type { TableProps } from 'antd'
import { useEffect, useState } from 'react'
import {
  getOpenMovieDiagnostics,
  type OpenMovieDiagnostics,
} from '../api/evalOpen'

const NA = '暂无'

interface AuditRow {
  key: string
  tag: string
  scope: string | null
  relevance: number | null
  semantics: number | null
  value: number | null
  inDir: boolean | null
  reason: string
  quote: string
  outcome: string
  kept: boolean
}

function score(value: number | null | undefined): string {
  return value === null || value === undefined ? 'null' : String(value)
}

/** 审计 → 逐候选行：判决按标签名对齐，去向按 accepted / dropped / 配额截断。 */
function auditRows(audit: NonNullable<OpenMovieDiagnostics['audit']>): AuditRow[] {
  const verdicts = new Map((audit.verdicts ?? []).map((row) => [row.tag, row]))
  const dropped = new Map((audit.dropped ?? []).map((row) => [row.tag, row.why]))
  const kept = new Map((audit.accepted ?? []).map((row) => [row.candidate ?? row.tag, row.tag]))
  const truncated = new Set((audit.library?.truncated ?? []).map((row) => row.tag))
  return (audit.merged ?? []).map((candidate, index) => {
    const verdict = verdicts.get(candidate.tag)
    const finalName = kept.get(candidate.tag)
    const why = dropped.get(candidate.tag)
    const outcome = finalName !== undefined
      ? (finalName === candidate.tag ? '保留' : `保留（库内标准名：${finalName}）`)
      : why ?? (truncated.has(candidate.tag) ? '超出队列配额' : '未进入产出')
    return {
      key: `${candidate.tag}-${index}`,
      tag: candidate.tag,
      scope: candidate.scope ?? null,
      relevance: verdict?.relevance ?? null,
      semantics: verdict?.semantics ?? null,
      value: verdict?.value ?? null,
      inDir: verdict?.in_dir ?? null,
      reason: verdict?.reason ?? '',
      quote: verdict?.quote ?? '',
      outcome,
      kept: finalName !== undefined,
    }
  })
}

const COLUMNS: TableProps<AuditRow>['columns'] = [
  { title: '候选标签', dataIndex: 'tag' },
  {
    title: '层级',
    render: (_, row) => (row.scope === 'scene' ? '场景级' : row.scope === 'film' ? '全片级' : NA),
  },
  { title: '内容支持', render: (_, row) => score(row.relevance) },
  { title: '名称清晰', render: (_, row) => score(row.semantics) },
  { title: '标签价值', render: (_, row) => score(row.value) },
  {
    title: '目录契合',
    render: (_, row) => (row.inDir === null ? NA : row.inDir ? '属于' : '不属于'),
  },
  {
    title: '去向',
    render: (_, row) => (row.kept
      ? <Tag color="green">{row.outcome}</Tag>
      : <Typography.Text type="secondary">{row.outcome}</Typography.Text>),
  },
]

export default function OpenEvalMovieAudit({ runId, tt }: { runId: string; tt: string }) {
  const [data, setData] = useState<OpenMovieDiagnostics | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    getOpenMovieDiagnostics(runId, tt)
      .then((body) => { if (alive) setData(body) })
      .catch((exc: unknown) => {
        if (alive) setError(exc instanceof Error ? exc.message : String(exc))
      })
    return () => { alive = false }
  }, [runId, tt])

  if (error) return <Alert type="warning" showIcon message={`无法读取生产审计：${error}`} />
  if (!data) return <Spin size="small" />

  const audit = data.audit
  const review = data.review
  const rows = audit ? auditRows(audit) : []
  const failedBatches = (review?.batches ?? []).filter((batch) => batch.error)

  return (
    <Space direction="vertical" style={{ display: 'flex' }}>
      {audit?.error && <Alert type="error" showIcon message={`生产中断：${audit.error}`} />}
      {audit ? (
        <>
          <Typography.Text type="secondary">
            合并候选 {audit.merged?.length ?? 0} 条 · judge 判决 {audit.verdicts?.length ?? 0} 条
            {' '}· 最终保留 {audit.accepted?.length ?? 0} 条
          </Typography.Text>
          {audit.judge_trace?.errors?.length ? (
            <Alert type="warning" showIcon
              message={`judge 响应曾不合协议：${audit.judge_trace.errors[0].error}`} />
          ) : null}
          {rows.length > 0 ? (
            <Table<AuditRow>
              size="small" rowKey="key" columns={COLUMNS} dataSource={rows}
              pagination={false} scroll={{ x: 720 }}
              expandable={{
                expandedRowRender: (row) => (
                  <Descriptions size="small" column={1} bordered
                    items={[
                      { key: 'reason', label: 'judge 理由', children: row.reason || NA },
                      { key: 'quote', label: '原文摘录', children: row.quote || NA },
                    ]} />
                ),
              }}
            />
          ) : <Typography.Text type="secondary">该片没有合并候选（生成阶段就没有产出）</Typography.Text>}
        </>
      ) : <Typography.Text type="secondary">该片没有生产审计（produce 失败或未落盘）</Typography.Text>}
      {review?.error && (
        <Alert type="warning" showIcon message={`评审失败：${review.error}`} />
      )}
      {failedBatches.length > 0 && (
        <Typography.Text type="secondary">
          评审批次失败：{failedBatches.map((batch) => `${batch.batch}#${batch.attempt} ${batch.error}`).join('；')}
        </Typography.Text>
      )}
    </Space>
  )
}
