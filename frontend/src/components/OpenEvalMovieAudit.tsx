// 单片生产审计与逐次模型响应，展开报告面板时取数。judge trace 双形状
// （评审归一 Task 10）：新键族 = 统一引擎 batches/errors dict/salvaged，
// legacy = 旧 run 的 raw/errors 列表——按键存在性分流渲染，缺键「暂无」。
import { Alert, Descriptions, Space, Spin, Table, Tag, Typography } from 'antd'
import type { TableProps } from 'antd'
import { useEffect, useState } from 'react'
import {
  getOpenMovieDiagnostics,
  type OpenMovieAudit,
  type OpenMovieDiagnostics,
} from '../api/evalOpen'

const NA = '暂无'

interface AuditRow {
  key: string
  tag: string
  scope: string | null
  stage?: number
  relevance: number | null
  semantics: number | null
  value: number | null
  valueBreakdown: string
  directory: string
  checks: string
  hasVerdict: boolean
  reason: string
  quote: string
  outcome: string
  kept: boolean
}

const KIND_LABELS: Record<string, string> = {
  content: '内容级',
  scene: '场景级',
  character: '人物级',
}

function score(value: number | null | undefined): string {
  if (value === null || value === undefined) return 'null'
  return Number.isInteger(value) ? String(value) : value.toFixed(2)
}

function formatValueScores(
  scores: Record<string, number> | undefined,
  tagKind: string | undefined,
): string {
  if (!scores) return ''
  const parts = [
    `内容呈现 ${score(scores.presentation)}`,
    `内容搜索 ${score(scores.search)}`,
    `内容推荐 ${score(scores.recommendation)}`,
  ].join(' · ')
  const kind = tagKind ? KIND_LABELS[tagKind] ?? tagKind : ''
  return kind ? `${kind}：${parts}` : parts
}

/** 新审计按候选编号对齐，旧审计仍可按标签名读取。 */
function auditRows(audit: NonNullable<OpenMovieDiagnostics['audit']>): AuditRow[] {
  const verdicts = new Map((audit.verdicts ?? []).map((row) => [row.candidate_id ?? row.tag, row]))
  const dropped = new Map((audit.dropped ?? []).map((row) => [row.candidate_id ?? row.tag, row.why]))
  const kept = new Map((audit.accepted ?? []).map((row) => [row.candidate_id ?? row.candidate ?? row.tag, row]))
  const truncated = new Set((audit.library?.truncated ?? []).map((row) => row.candidate_id ?? row.tag))
  const merged: NonNullable<typeof audit.merged> =
    audit.merged ?? (audit.candidates ?? []).map((tag) => ({ tag, scope: null }))
  const candidateNames = new Map(merged.map((row) => [row.candidate_id ?? row.tag, row.tag]))
  const clusterOutcomes = new Map(audit.clustering?.clusters.flatMap(({ representative, members }) => {
    const name = candidateNames.get(representative)
    const outcome = name ? `聚类未选为代表（代表标签：${name}）` : '聚类未选为代表'
    return members
      .filter((id) => id !== representative)
      .map((id) => [id, outcome] as const)
  }))
  return merged.map((candidate, index) => {
    const id = candidate.candidate_id ?? candidate.tag
    const verdict = verdicts.get(id)
    const final = kept.get(id)
    const why = dropped.get(id)
    const unassigned = audit.error ? '生产中断' : '未进入产出'
    const outcome = final
      ? (final.tag === candidate.tag ? '保留' : `保留（库内标准名：${final.tag}）`)
      : why ?? (truncated.has(id) ? '超出队列配额' : clusterOutcomes.get(id) ?? unassigned)
    const fits = verdict?.dir_fits ?? {}
    const targets = audit.directory_targets?.[id] ?? Object.keys(fits)
    const checks = targets.map((id) => `${id} ${directoryFit(fits[id])}`).join(' · ')
    const displayedFit = final?.dir_id ? fits[final.dir_id] ?? verdict?.in_dir : verdict?.in_dir
    const directory = final?.origin === 'catalog'
      ? '库内复用'
      : displayedFit == null && Object.keys(fits).length ? '见目录判决' : directoryFit(displayedFit)
    return {
      key: candidate.candidate_id ?? `${candidate.tag}-${index}`,
      tag: candidate.tag,
      scope: candidate.scope ?? null,
      stage: candidate.stage,
      relevance: verdict?.relevance ?? null,
      semantics: verdict?.semantics ?? null,
      value: verdict?.value ?? null,
      valueBreakdown: formatValueScores(verdict?.value_scores, verdict?.tag_kind),
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

/** judge_trace 读侧视图：responses 喂 ResponseDetails；新键族另带 errors
 * dict 键值对（最终未获判决目标）与 salvaged；legacy 只计失败调用次数。 */
interface JudgeTraceView {
  responses: ResponseRow[]
  failedTargets: [string, string][]
  failedCalls: number
  salvaged: number
}

const EMPTY_JUDGE_TRACE: JudgeTraceView = {
  responses: [], failedTargets: [], failedCalls: 0, salvaged: 0,
}

/** judge_trace 双形状按键分流（评审归一 Task 10）：新键族（统一引擎
 * batches/errors dict/salvaged）逐批现场——注意 errors 恒为 dict，旧读法
 * `errors?.map` 对新审计直接 TypeError（整页白屏），故按键存在性分流；
 * legacy（旧 run raw/errors 列表）按 attempt 排序照旧。缺键 → 空视图。 */
function judgeTraceView(trace: OpenMovieAudit['judge_trace']): JudgeTraceView {
  if (!trace) return EMPTY_JUDGE_TRACE
  const salvaged = trace.salvaged ?? 0
  if ('batches' in trace) {
    return {
      responses: trace.batches.map((batch) => ({
        label: `${batch.batch}#${batch.attempt}${batch.targets?.length ? ` · ${batch.targets.join('、')}` : ''}`,
        raw: batch.raw,
        error: batch.missing?.length
          ? `${batch.error ?? ''}（缺少 ${batch.missing.join('、')}）`
          : batch.error ?? undefined,
      })),
      failedTargets: Object.entries(trace.errors ?? {}),
      failedCalls: 0,
      salvaged,
    }
  }
  const raw = new Map((trace.raw ?? []).map(({ attempt, content }) => [attempt, content]))
  const errors = new Map((trace.errors ?? []).map(({ attempt, error }) => [attempt, error]))
  return {
    responses: [...new Set([...raw.keys(), ...errors.keys()])]
      .sort((a, b) => a - b)
      .map((attempt) => ({
        label: `judge 第 ${attempt + 1} 次`, raw: raw.get(attempt), error: errors.get(attempt),
      })),
    failedTargets: [],
    failedCalls: errors.size,
    salvaged,
  }
}

function buildColumns(showLegacyDirFit: boolean): TableProps<AuditRow>['columns'] {
  return [
    { title: '候选标签', dataIndex: 'tag' },
    {
      title: '生成来源',
      render: (_, row) => row.stage
        ? (row.stage === 1 ? '一阶段' : '二阶段')
        : (row.scope === 'scene' ? '场景级' : row.scope === 'film' ? '全片级' : NA),
    },
    { title: '内容相关性', render: (_, row) => row.hasVerdict ? score(row.relevance) : '未判' },
    { title: '语义准确性', render: (_, row) => row.hasVerdict ? score(row.semantics) : '未判' },
    { title: '标签价值', render: (_, row) => row.hasVerdict ? score(row.value) : '未判' },
    ...(showLegacyDirFit
      ? [{ title: '目录契合', render: (_: unknown, row: AuditRow) => row.directory }]
      : []),
    {
      title: '去向',
      render: (_, row) => (row.kept
        ? <Tag color="green">{row.outcome}</Tag>
        : <Typography.Text type="secondary">{row.outcome}</Typography.Text>),
    },
  ]
}

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
  const showLegacyDirFit = (audit?.verdicts ?? []).some(
    (row) => (row.dir_fits && Object.keys(row.dir_fits).length > 0) || row.in_dir !== undefined,
  )
  const columns = buildColumns(showLegacyDirFit)
  const candidateLabel = rows.some((row) => row.stage) ? '送审候选' : '合并候选'
  const judgeTrace = judgeTraceView(audit?.judge_trace)
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
            {candidateLabel} {rows.length} 条 · judge 判决 {audit.verdicts?.length ?? 0} 条
            {' '}· 最终保留 {audit.accepted?.length ?? 0} 条
          </Typography.Text>
          {judgeTrace.failedCalls > 0 && (
            <Alert type="warning" showIcon message={`judge 有 ${judgeTrace.failedCalls} 次调用出现异常`} />
          )}
          {judgeTrace.failedTargets.length > 0 && (
            <Alert type="warning" showIcon
              message={`judge 有 ${judgeTrace.failedTargets.length} 个候选未获判决`} />
          )}
          {judgeTrace.failedTargets.length > 0 && (
            <Typography.Text type="secondary">
              judge 未完成：{judgeTrace.failedTargets.map(([key, reason]) => `${key} ${reason}`).join('；')}
            </Typography.Text>
          )}
          {judgeTrace.salvaged > 0 && (
            <Typography.Text type="secondary">漏判恢复 {judgeTrace.salvaged} 条</Typography.Text>
          )}
          {rows.length > 0 ? (
            <Table<AuditRow>
              size="small" rowKey="key" columns={columns} dataSource={rows}
              pagination={rows.length > 50 ? { pageSize: 50, showSizeChanger: false } : false}
              scroll={{ x: 720 }}
              expandable={{
                expandedRowRender: (row) => (
                  <Descriptions size="small" column={1} bordered
                    items={[
                      { key: 'reason', label: 'judge 理由', children: row.reason || NA },
                      { key: 'quote', label: '原文摘录', children: row.quote || NA },
                      ...(row.valueBreakdown ? [{ key: 'value', label: '价值明细', children: row.valueBreakdown }] : []),
                      ...(showLegacyDirFit && row.checks ? [{ key: 'dirs', label: '目录判决', children: row.checks }] : []),
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
      <ResponseDetails title="judge 原始响应" responses={judgeTrace.responses} />
      <ResponseDetails title="独立评审原始响应" responses={reviewResponses} />
    </Space>
  )
}
