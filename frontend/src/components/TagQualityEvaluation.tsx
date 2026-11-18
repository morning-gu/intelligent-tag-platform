import { Alert, Button, Card, Descriptions, Input, Modal, Space, Table, Tag, Typography } from 'antd'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  DEFAULT_QUALITY_MODEL,
  retryQualityEvaluation,
  type FilmQuality,
  type QualityDimension,
  type QualityLabel,
  type QualityMetrics,
  type QualityReport,
  type QualityScore,
  type QualityStatus,
} from '../api/tasks'

const statuses: Record<QualityStatus, { color: string; label: string }> = {
  queued: { color: 'default', label: '等待评估' },
  running: { color: 'processing', label: '评估中' },
  completed: { color: 'success', label: '已完成' },
  partial: { color: 'warning', label: '部分完成' },
  failed: { color: 'error', label: '评估失败' },
  not_needed: { color: 'default', label: '无需评估' },
  generation_failed: { color: 'error', label: '生成失败' },
  unavailable: { color: 'default', label: '无评估记录' },
}

function EvaluationStatus({ status }: { status: QualityStatus }) {
  const { color, label } = statuses[status]
  return <Tag color={color}>{label}</Tag>
}

function percent(value: number | null): string {
  return value === null ? '—' : `${(100 * value).toFixed(1)}%`
}

function DimensionSummary({ dimension }: { dimension: QualityDimension }) {
  return (
    <Space direction="vertical" size={0}>
      <span>均分 {dimension.average?.toFixed(2) ?? '—'} · 高分率 {percent(dimension.high_rate)}</span>
      <Typography.Text type="secondary">
        0 / 1 / 2 分各 {dimension.distribution.join(' / ')} 条 ·
        无法判断 {dimension.undetermined}（{percent(dimension.undetermined_rate)}）
      </Typography.Text>
    </Space>
  )
}

function Metrics({ metrics }: { metrics: QualityMetrics }) {
  return (
    <Descriptions size="small" column={{ xs: 1, sm: 2 }} items={[
      {
        key: 'tags', label: '已评估 / 最终标签',
        children: `${metrics.evaluated_tags} / ${metrics.total_tags} 条`,
      },
      { key: 'coverage', label: '评估覆盖率', children: percent(metrics.coverage) },
      { key: 'relevance', label: '内容相关性', children: <DimensionSummary dimension={metrics.relevance} /> },
      { key: 'semantics', label: '语义准确性', children: <DimensionSummary dimension={metrics.semantics} /> },
      { key: 'joint', label: '双维联合合格率', children: percent(metrics.joint_high_rate) },
      {
        key: 'unknown', label: '任一维度无法判断',
        children: `${metrics.undetermined_tags} 条（${percent(metrics.undetermined_rate)}）`,
      },
    ]} />
  )
}

function ScoreTag({ result }: { result?: QualityScore }) {
  if (result === undefined) return <Tag>未完成</Tag>
  const color = result.score === null ? 'default' : ['error', 'warning', 'success'][result.score]
  return <Tag color={color}>{result.score === null ? '无法判断' : `${result.score} 分`}</Tag>
}

function LabelDetail({ label, assetId }: { label: QualityLabel; assetId: string }) {
  const score = label.score
  return (
    <Descriptions size="small" column={1} items={[
      { key: 'desc', label: '最终释义', children: label.desc || '暂无释义' },
      ...(score ? [
        { key: 'support', label: '相关性理由', children: score.relevance.reason },
        { key: 'meaning', label: '语义理由', children: score.semantics.reason },
        ...(score.evidence.length ? [{
          key: 'evidence', label: '内容证据',
          children: (
            <Space direction="vertical" size={2}>
              {score.evidence.map(({ scene_id, start_ms, description }, index) => (
                <span key={index}>
                  {scene_id && start_ms !== null && (
                    <>
                      <Link to={`/assets/${assetId}?scene=${encodeURIComponent(scene_id)}&t=${start_ms}`}>{scene_id}</Link>{' '}
                    </>
                  )}
                  {description}
                </span>
              ))}
            </Space>
          ),
        }] : []),
      ] : []),
    ]} />
  )
}

function FilmEvaluation({ film, onRetry }: { film: FilmQuality; onRetry: () => void }) {
  const [showLabels, setShowLabels] = useState(false)
  return (
    <Card
      size="small"
      title={<Space wrap><span>{film.title}</span><EvaluationStatus status={film.status} /></Space>}
      extra={['failed', 'partial'].includes(film.status) && (
        <Button size="small" onClick={onRetry}>重试评估</Button>
      )}
    >
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        {film.model && <Typography.Text type="secondary">
          模型 {film.model}{film.prompt_version && ` · 提示词 ${film.prompt_version}`}
        </Typography.Text>}
        {film.error && <Alert type="error" showIcon message={film.error} />}
        {film.note && <Alert type="info" showIcon message={film.note} />}
        {film.metrics && <Metrics metrics={film.metrics} />}
        {film.status === 'not_needed' && <Typography.Text type="secondary">最终标签 0 条</Typography.Text>}
        {film.labels.length > 0 && (
          <details onToggle={(event) => setShowLabels(event.currentTarget.open)}>
            <summary style={{ cursor: 'pointer' }}>标签评分（{film.labels.length} 条）</summary>
            {showLabels && <Table<QualityLabel>
              size="small"
              rowKey="id"
              pagination={film.labels.length > 15 ? { pageSize: 15, hideOnSinglePage: true } : false}
              dataSource={film.labels}
              columns={[
                { title: '标签', dataIndex: 'name' },
                { title: '目录', dataIndex: 'dir_path', render: (path) => path || '—' },
                {
                  title: '生产分流',
                  render: (_, label) => label.disposition === 'mounted' ? '直接挂载' : '进入审核',
                },
                { title: '内容相关性', render: (_, label) => <ScoreTag result={label.score?.relevance} /> },
                { title: '语义准确性', render: (_, label) => <ScoreTag result={label.score?.semantics} /> },
              ]}
              expandable={{ expandedRowRender: (label) => <LabelDetail label={label} assetId={film.asset_id} /> }}
            />}
          </details>
        )}
      </Space>
    </Card>
  )
}

export default function TagQualityEvaluation({ report, onRetry }: {
  report: QualityReport
  onRetry: () => void
}) {
  const [retryFilm, setRetryFilm] = useState<FilmQuality | null>(null)
  const [model, setModel] = useState(DEFAULT_QUALITY_MODEL)
  const [retrying, setRetrying] = useState(false)
  const [retryError, setRetryError] = useState<string | null>(null)
  const counts = report.film_counts

  async function retry() {
    if (retryFilm === null || !model.trim()) return
    setRetrying(true)
    setRetryError(null)
    try {
      await retryQualityEvaluation(retryFilm.task_id, model.trim())
      setRetryFilm(null)
      onRetry()
    } catch (err) {
      setRetryError(err instanceof Error ? err.message : '评估重试失败')
    } finally {
      setRetrying(false)
    }
  }

  return (
    <Card size="small" title="标签质量评估" extra={<EvaluationStatus status={report.status} />}>
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Space wrap>
          <Typography.Text strong>任务汇总（{report.films.length} 部影片）</Typography.Text>
          {Object.entries(counts)
            .filter(([, count]) => count > 0)
            .map(([status, count]) => (
              <span key={status}>{statuses[status as QualityStatus].label} {count}</span>
            ))}
          {report.incomplete && <Typography.Text type="secondary">汇总结果尚未完整</Typography.Text>}
        </Space>
        {report.status !== 'unavailable' && <Metrics metrics={report.metrics} />}
        {report.films.map((film) => (
          <FilmEvaluation key={film.task_id} film={film} onRetry={() => {
            setRetryFilm(film)
            setModel(film.model ?? DEFAULT_QUALITY_MODEL)
            setRetryError(null)
          }} />
        ))}
      </Space>
      <Modal
        title="重试影片评估"
        open={retryFilm !== null}
        confirmLoading={retrying}
        okText="重试"
        cancelText="取消"
        okButtonProps={{ disabled: !model.trim() }}
        onOk={() => void retry()}
        onCancel={() => setRetryFilm(null)}
      >
        <label htmlFor="quality-retry-model">评估模型 ID</label>
        <Input
          id="quality-retry-model"
          value={model}
          maxLength={200}
          onChange={(event) => setModel(event.target.value)}
        />
        {retryError && <Alert type="error" showIcon message={retryError} />}
      </Modal>
    </Card>
  )
}
