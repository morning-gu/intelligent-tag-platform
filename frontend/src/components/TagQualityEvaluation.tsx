import { Alert, Button, Card, Collapse, Descriptions, Input, Modal, Space, Tag, Typography } from 'antd'
import { useState, type ReactNode } from 'react'
import {
  DEFAULT_QUALITY_MODEL,
  retryQualityEvaluation,
  type FilmQuality,
  type QualityDimension,
  type QualityMetrics,
  type QualityReport,
  type QualityStatus,
} from '../api/tasks'
import TaskTagResults from './TaskTagResults'
import QualityRubricHelp from './QualityRubricHelp'

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

function DimensionSummary({ dimension, maxScore }: { dimension: QualityDimension; maxScore: number }) {
  const scoreLevels = dimension.distribution.map((_, score) => score).join(' / ')
  return (
    <Space orientation="vertical" size={0}>
      <span>均分 {dimension.average?.toFixed(2) ?? '—'} / {maxScore} · 满分率 {percent(dimension.high_rate)}</span>
      <Typography.Text type="secondary">
        {scoreLevels} 分各 {dimension.distribution.join(' / ')} 条 ·
        无法判断 {dimension.undetermined}（{percent(dimension.undetermined_rate)}）
      </Typography.Text>
    </Space>
  )
}

export function QualityMetricsDetail({ metrics }: { metrics: QualityMetrics }) {
  const maxScore = metrics.max_score === undefined ? 2 : metrics.max_score
  const scoreItems = maxScore === null ? [{
    key: 'scale', label: '评分量表', children: '本批次使用了不同量表，分数统计见各影片。',
  }] : [
    { key: 'relevance', label: '内容相关性', children: <DimensionSummary dimension={metrics.relevance} maxScore={maxScore} /> },
    { key: 'semantics', label: '语义准确性', children: <DimensionSummary dimension={metrics.semantics} maxScore={maxScore} /> },
    {
      key: 'value', label: '标签价值',
      children: metrics.value.evaluated_tags === 0 ? '未评估' : (
        <Space orientation="vertical" size={0}>
          <DimensionSummary dimension={metrics.value} maxScore={maxScore} />
          <Typography.Text type="secondary">
            已评估 {metrics.value.evaluated_tags} / {metrics.total_tags} 条
          </Typography.Text>
        </Space>
      ),
    },
    { key: 'joint', label: '双维同时满分率', children: percent(metrics.joint_high_rate) },
    { key: 'joint-three', label: '三维同时满分率', children: percent(metrics.three_dimension_high_rate) },
  ]
  return (
    <Descriptions size="small" column={{ xs: 1, sm: 2 }} items={[
      {
        key: 'tags', label: '已评估 / 最终标签',
        children: `${metrics.evaluated_tags} / ${metrics.total_tags} 条`,
      },
      { key: 'coverage', label: '评估覆盖率', children: percent(metrics.coverage) },
      ...scoreItems,
      {
        key: 'unknown', label: '任一维度无法判断',
        children: `${metrics.undetermined_tags} 条（${percent(metrics.undetermined_rate)}）`,
      },
    ]} />
  )
}

function MetricsSummary({ metrics }: { metrics: QualityMetrics }) {
  const maxScore = metrics.max_score ?? 2
  const average = (dimension: QualityDimension) =>
    dimension.average === null ? '—' : `${dimension.average.toFixed(2)} / ${maxScore}`
  const items = [
    ['已评估 / 最终标签', `${metrics.evaluated_tags} / ${metrics.total_tags} 条`],
    ['内容相关性均分', average(metrics.relevance)],
    ['语义准确性均分', average(metrics.semantics)],
    ['标签价值均分', average(metrics.value)],
  ]
  return (
    <dl className="task-quality-summary">
      {items.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
    </dl>
  )
}

function FilmEvaluation({ film, onRetry, children }: {
  film: FilmQuality
  onRetry: () => void
  children: ReactNode
}) {
  return (
    <div className="task-quality-body">
      <Space wrap>
        <Typography.Text strong>{film.title}</Typography.Text>
        <EvaluationStatus status={film.status} />
        {film.metrics && (
          <Tag color={film.metrics.max_score === 4 ? 'blue' : 'default'}>
            {film.metrics.max_score === 4 ? '五档 0–4' : '三档 0–2'}
          </Tag>
        )}
        {film.model && <Typography.Text type="secondary">
          评估模型 {film.model}{film.prompt_version && ` · ${film.prompt_version}`}
        </Typography.Text>}
        {['failed', 'partial'].includes(film.status) && (
          <Button size="small" onClick={onRetry}>重试评估</Button>
        )}
      </Space>
      {film.error && <Alert type="error" showIcon title={film.error} />}
      {film.note && <Alert type="info" showIcon title={film.note} />}
      {film.metrics && (
        <>
          <MetricsSummary metrics={film.metrics} />
          <details className="task-quality-metrics">
            <summary>评分统计</summary>
            <div><QualityMetricsDetail metrics={film.metrics} /></div>
          </details>
        </>
      )}
      {children}
    </div>
  )
}

export default function TagQualityEvaluation({ report, taskId, onRetry, children }: {
  report: QualityReport
  taskId: string
  onRetry: () => void
  children: ReactNode
}) {
  const [retryFilm, setRetryFilm] = useState<FilmQuality | null>(null)
  const [model, setModel] = useState(DEFAULT_QUALITY_MODEL)
  const [retrying, setRetrying] = useState(false)
  const [retryError, setRetryError] = useState<string | null>(null)

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
  const current = report.films.find((film) => film.task_id === taskId)
  const siblings = report.films.filter((film) => film.task_id !== taskId)

  function openRetry(film: FilmQuality) {
    setRetryFilm(film)
    setModel(film.model ?? DEFAULT_QUALITY_MODEL)
    setRetryError(null)
  }

  return (
    <Card size="small" title="标签产出与评分" extra={<QualityRubricHelp />}>
      <div className="task-quality-body">
        {current ? <FilmEvaluation film={current} onRetry={() => openRetry(current)}>
          {children}
        </FilmEvaluation> : children}
        {siblings.length > 0 && (
          <Collapse size="small" items={[{
            key: 'batch',
            label: `同批次任务（另 ${siblings.length} 部影片）`,
            children: (
              <div className="task-quality-body">
                <Space wrap>
                  <Typography.Text strong>任务汇总（{report.films.length} 部影片）</Typography.Text>
                  <EvaluationStatus status={report.status} />
                  {Object.entries(report.film_counts).filter(([, count]) => count > 0)
                    .map(([status, count]) => <span key={status}>{statuses[status as QualityStatus].label} {count}</span>)}
                  {report.incomplete && <Typography.Text type="secondary">汇总结果尚未完整</Typography.Text>}
                </Space>
                {report.status !== 'unavailable' && <QualityMetricsDetail metrics={report.metrics} />}
                <Collapse size="small" items={siblings.map((film) => ({
                  key: film.task_id,
                  label: <Space wrap>{film.title}<EvaluationStatus status={film.status} /></Space>,
                  children: <FilmEvaluation film={film} onRetry={() => openRetry(film)}>
                    {film.labels.length > 0 && (
                      <TaskTagResults assetId={film.asset_id} labels={film.labels} results={[]}
                        maxScore={film.metrics?.max_score ?? 2} />
                    )}
                  </FilmEvaluation>,
                }))} />
              </div>
            ),
          }]} />
        )}
      </div>
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
        {retryError && <Alert type="error" showIcon title={retryError} />}
      </Modal>
    </Card>
  )
}
