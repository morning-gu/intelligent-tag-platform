// 单片生产审计与逐次模型响应，展开报告面板时取数。
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
  directory: string
  checks: string
  hasVerdict: boolean
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
  const kept = new Map((audit.accepted ?? []).map((row) => [row.candidate ?? row.tag, row]))
  const truncated = new Set((audit.library?.truncated ?? []).map((row) => row.tag))
  const merged = audit.merged ?? (audit.candidates ?? []).map((tag) => ({ tag, scope: null }))
  return merged.map((candidate, index) => {
    const verdict = verdicts.get(candidate.tag)
    const final = kept.get(candidate.tag)
    const why = dropped.get(candidate.tag)
    const unassigned = audit.error ? '生产中断' : '未进入产出'
    const outcome = final
      ? (final.tag === candidate.tag ? '保留' : `保留（库内标准名：${final.tag}）`)
      : why ?? (truncated.has(candidate.tag) ? '超出队列配额' : unassigned)
    const fits = verdict?.dir_fits ?? {}
    const targets = audit.directory_targets?.[candidate.tag] ?? Object.keys(fits)
    const checks = targets.map((id) => `${id} ${directoryFit(fits[id])}`).join(' · ')
    const displayedFit = final?.dir_id ? fits[final.dir_id] ?? verdict?.in_dir : verdict?.in_dir
    const directory = final?.origin === 'catalog'
      ? '库内复用'
      : displayedFit == null && Object.keys(fits).length ? '见目录判决' : directoryFit(displayedFit)
    return {
      key: `${candidate.tag}-${index}`,
      tag: candidate.tag,
      scope: candidate.scope ?? null,
      relevance: verdict?.relevance ?? null,
      semantics: verdict?.semantics ?? null,
      value: verdict?.value ?? null,
      directory,
      checks,
      hasVerdict: verdict !== undefined,
      reason: verdict?.reason ?? '',
      quote: verdict?.quote ?? '',
      outcome,
      kept: final !== undefined,
    }
  })
}

function directoryFit(value: boolean | null | undefined): string {
  if (value === true) return '属于'
  if (value === false) return '不属于'
  return '未判'
}

interface ResponseRow { label: string; raw?: string; error?: string }

function ResponseDetails({ title, responses }: { title: string; responses: ResponseRow[] }) {
  if (!responses.length) return null
  return (
    <details>
      <summary>{title}（{responses.length} 次）</summary>
      <Space direction="vertical" style={{ display: 'flex' }}>
        {responses.map(({ label, raw, error }, index) => (
          <details key={`${label}-${index}`}>
            <summary>{label}{error ? ` · ${error}` : ''}</summary>
            <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 320, overflow: 'auto' }}>
              {raw || '无响应文本'}
            </pre>
          </details>
        ))}
      </Space>
    </details>
  )
}

const COLUMNS: TableProps<AuditRow>['columns'] = [
  { title: '候选标签', dataIndex: 'tag' },
  {
    title: '层级',
    render: (_, row) => (row.scope === 'scene' ? '场景级' : row.scope === 'film' ? '全片级' : NA),
  },
  { title: '内容支持', render: (_, row) => row.hasVerdict ? score(row.relevance) : '未判' },
  { title: '名称清晰', render: (_, row) => row.hasVerdict ? score(row.semantics) : '未判' },
  { title: '标签价值', render: (_, row) => row.hasVerdict ? score(row.value) : '未判' },
  {
    title: '目录契合',
    render: (_, row) => row.directory,
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
  const judgeErrors = new Map(audit?.judge_trace?.errors?.map(({ attempt, error }) => [attempt, error]))
  const judgeRaw = new Map(audit?.judge_trace?.raw?.map(({ attempt, content }) => [attempt, content]))
  const judgeResponses = [...new Set([...judgeRaw.keys(), ...judgeErrors.keys()])]
    .sort((a, b) => a - b)
    .map((attempt) => ({
      label: `judge 第 ${attempt + 1} 次`, raw: judgeRaw.get(attempt), error: judgeErrors.get(attempt),
    }))
  const reviewResponses = (review?.batches ?? []).map((batch) => ({
    label: `${batch.batch}#${batch.attempt}${batch.targets?.length ? ` · ${batch.targets.join('、')}` : ''}`,
    raw: batch.raw,
    error: batch.missing?.length ? `${batch.error}（缺少 ${batch.missing.join('、')}）` : batch.error,
  }))
  const reviewErrors = Object.entries(review?.errors ?? {})

  return (
    <Space direction="vertical" style={{ display: 'flex' }}>
      {audit?.error && <Alert type="error" showIcon message={`生产中断：${audit.error}`} />}
      {audit ? (
        <>
          <Typography.Text type="secondary">
            合并候选 {rows.length} 条 · judge 判决 {audit.verdicts?.length ?? 0} 条
            {' '}· 最终保留 {audit.accepted?.length ?? 0} 条
          </Typography.Text>
          {judgeErrors.size > 0 && (
            <Alert type="warning" showIcon message={`judge 有 ${judgeErrors.size} 次调用出现异常`} />
          )}
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
                      ...(row.checks ? [{ key: 'dirs', label: '目录判决', children: row.checks }] : []),
                    ]} />
                ),
              }}
            />
          ) : <Typography.Text type="secondary">该片没有候选记录</Typography.Text>}
        </>
      ) : <Typography.Text type="secondary">该片没有生产审计（produce 失败或未落盘）</Typography.Text>}
      {review?.error && (
        <Alert type="warning" showIcon message={`评审失败：${review.error}`} />
      )}
      {reviewErrors.length > 0 && (
        <Typography.Text type="secondary">
          评审未完成：{reviewErrors.map(([id, reason]) => `${id} ${reason}`).join('；')}
        </Typography.Text>
      )}
      <ResponseDetails title="judge 原始响应" responses={judgeResponses} />
      <ResponseDetails title="独立评审原始响应" responses={reviewResponses} />
    </Space>
  )
}
