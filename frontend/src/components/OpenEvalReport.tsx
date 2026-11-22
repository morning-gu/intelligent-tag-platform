import { Alert, Collapse, Descriptions, Space, Table, Tag, Typography } from 'antd'
import type { TableProps } from 'antd'
import { openEvalApi, type OpenEvalLabel, type OpenEvalReport } from '../api/eval'
import type { QualityDimension, QualityMetrics, QualityScore, QualityStatus } from '../api/tasks'
import BarChart from './BarChart'
import EvalReportSummary, { coverageBars } from './EvalReportSummary'
import QualityRubricHelp from './QualityRubricHelp'

const dimensions = [
  { key: 'relevance', title: '内容相关性' },
  { key: 'semantics', title: '语义准确性' },
  { key: 'value', title: '标签价值' },
] as const

const filmStatuses: Partial<Record<QualityStatus, string>> = {
  completed: '评估完成',
  not_needed: '未产出标签',
  partial: '部分标签已评分',
  failed: '评分失败',
  generation_failed: '生成失败',
}

function percent(value: number | null) {
  return value === null ? '暂无' : `${(value * 100).toFixed(1)}%`
}

function average(value: number | null, max: number) {
  return value === null ? null : `${value.toFixed(2)} / ${max}`
}

type DimensionRow = QualityDimension & { key: string; title: string }

function ScoreDistribution({ metrics }: { metrics: QualityMetrics }) {
  const max = metrics.max_score ?? 4
  const rows = dimensions.map(({ key, title }) => ({ key, title, ...metrics[key] }))
  const columns: TableProps<DimensionRow>['columns'] = [
    { title: '评分维度', dataIndex: 'title' },
    { title: '均分', render: (_, row) => average(row.average, max) ?? '暂无' },
    { title: '满分率', render: (_, row) => percent(row.high_rate) },
    ...Array.from({ length: max + 1 }, (_, score) => ({
      key: score,
      title: `${score} 分`,
      render: (_: unknown, row: DimensionRow) => row.distribution[score] ?? '—',
    })),
    { title: '无法判断', dataIndex: 'undetermined' },
  ]
  return (
    <>
      <Table<DimensionRow>
        rowKey="key"
        columns={columns}
        dataSource={rows}
        size="small"
        pagination={false}
        scroll={{ x: 680 }}
      />
      <Space wrap size="large">
        <Typography.Text type="secondary">
          内容与语义同时满分 {percent(metrics.joint_high_rate)}
        </Typography.Text>
        <Typography.Text type="secondary">
          三维同时满分 {percent(metrics.three_dimension_high_rate)}
        </Typography.Text>
      </Space>
    </>
  )
}

function ScoreCell({ rating }: { rating: QualityScore | null | undefined }) {
  if (!rating) return <Typography.Text type="secondary">—</Typography.Text>
  return (
    <Space orientation="vertical" size={0}>
      <Typography.Text strong>{rating.score === null ? '无法判断' : `${rating.score} / 4`}</Typography.Text>
      <Typography.Text type="secondary">{rating.reason}</Typography.Text>
    </Space>
  )
}

const labelColumns: TableProps<OpenEvalLabel>['columns'] = [
  {
    title: '标签',
    dataIndex: 'name',
    render: (name: string, label) => (
      <Space orientation="vertical" size={0}>
        <Typography.Text strong>{name}</Typography.Text>
        {label.desc && <Typography.Text type="secondary">{label.desc}</Typography.Text>}
      </Space>
    ),
    width: '22%',
  },
  {
    title: '层级',
    dataIndex: 'level',
    render: (level: OpenEvalLabel['level']) => <Tag>{level === 'scene' ? '场景级' : '影片级'}</Tag>,
  },
  { title: '内容相关性', render: (_, label) => <ScoreCell rating={label.score?.relevance} /> },
  { title: '语义准确性', render: (_, label) => <ScoreCell rating={label.score?.semantics} /> },
  { title: '标签价值', render: (_, label) => <ScoreCell rating={label.score?.value} /> },
]

export default function OpenEvalReportView({ report, runId }: { report: OpenEvalReport; runId: string }) {
  const { meta, films, metrics } = report
  const coverage = films.flatMap(({ metrics }) => metrics?.coverage == null ? [] : [metrics.coverage])
  const issues = films.filter((film) => ['partial', 'failed', 'generation_failed'].includes(film.status))
  const max = metrics.max_score ?? 4
  const summary = [
    { key: 'movies', title: '已处理影片', value: `${meta.done} / ${meta.total}` },
    { key: 'scored', title: '已评分标签', value: `${metrics.evaluated_tags} / ${metrics.total_tags}` },
    ...dimensions.map(({ key, title }) => ({ key, title, value: average(metrics[key].average, max) })),
    { key: 'coverage', title: '评分覆盖率', value: percent(metrics.coverage) },
  ]
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {report.incomplete && <Alert type="warning" showIcon title="部分结果" description="汇总覆盖已产出的标签。请结合影片状态及评分覆盖率查看。" />}
      {(meta.mock_llm || meta.mock_embedder) && <Alert type="info" showIcon title="本次运行使用了模拟模型" />}
      <EvalReportSummary items={summary} />
      <Space wrap>
        {Object.entries(report.film_counts).map(([status, count]) => (
          <Tag key={status}>{filmStatuses[status as QualityStatus] ?? status} {count} 部</Tag>
        ))}
      </Space>
      <section aria-label="评分分布">
        <Typography.Title level={5}>评分分布</Typography.Title>
        <ScoreDistribution metrics={metrics} />
      </section>
      <section aria-label="影片评分覆盖分布">
        <Typography.Title level={5}>影片评分覆盖分布</Typography.Title>
        {coverage.length > 0 ? (
          <>
            <BarChart items={coverageBars(coverage)} />
            <Typography.Text type="secondary">覆盖率为已评分标签数 ÷ 产出标签数，统计有产出且可计算的 {coverage.length} 部影片。</Typography.Text>
          </>
        ) : <Typography.Text type="secondary">暂无可计算覆盖率的影片</Typography.Text>}
      </section>
      {films.length > 0 && (
        <section aria-label="逐片结果">
          <Typography.Title level={5}>逐片结果</Typography.Title>
          <Collapse items={films.map((film) => ({
            key: film.tt,
            label: (
              <Space wrap>
                <Typography.Text strong>{film.title}</Typography.Text>
                <Typography.Text type="secondary">{film.tt}</Typography.Text>
                <Tag>{filmStatuses[film.status]}</Tag>
                <Typography.Text type="secondary">{film.produced.length} 条标签 · {film.frame_count} 张帧</Typography.Text>
              </Space>
            ),
            children: (
              <Space orientation="vertical" style={{ display: 'flex' }}>
                {film.error && <Alert type="error" showIcon title={film.error} />}
                {film.note && <Alert type="info" showIcon title={film.note} />}
                {film.metrics && <ScoreDistribution metrics={film.metrics} />}
                {film.produced.length > 0 && (
                  <Table<OpenEvalLabel>
                    size="small"
                    rowKey="id"
                    columns={labelColumns}
                    dataSource={film.produced}
                    pagination={false}
                    scroll={{ x: 680 }}
                    expandable={{
                      expandedRowRender: (label) => (
                        <Descriptions size="small" column={1} items={[
                          { key: 'reason', label: '生成理由', children: label.reason ?? '—' },
                          {
                            key: 'evidence',
                            label: '生成证据',
                            children: label.evidence.map((item) => (
                              <div key={item.scene_id}>{item.scene_id} · {item.description}</div>
                            )),
                          },
                          {
                            key: 'evaluation',
                            label: '评分依据',
                            children: label.score?.evidence.map((item, index) => (
                              <div key={index}>{item.scene_id && `${item.scene_id} · `}{item.description}</div>
                            )),
                          },
                        ]} />
                      ),
                    }}
                  />
                )}
              </Space>
            ),
          }))} />
        </section>
      )}
      {issues.length > 0 && (
        <section aria-label="处理异常">
          <Typography.Title level={5}>处理异常（{issues.length} 部）</Typography.Title>
          <Table
            rowKey="tt"
            columns={[
              { title: '影片', dataIndex: 'title' },
              { title: '状态', render: (_, film) => filmStatuses[film.status] },
              { title: '原因', render: (_, film) => film.error ?? '暂无' },
            ]}
            dataSource={issues}
            pagination={false}
            size="small"
          />
        </section>
      )}
      <section aria-label="产物下载">
        <Typography.Title level={5}>产物下载</Typography.Title>
        <Space orientation="vertical" size={4}>
          <a href={openEvalApi.fileUrl(runId, 'report.json')} download>report.json</a>
          <a href={openEvalApi.fileUrl(runId, 'raw_results.json')} download>raw_results.json</a>
        </Space>
      </section>
      <Alert
        type="info"
        showIcon
        title="评估口径"
        description={(
          <Space orientation="vertical" size={4}>
            <Typography.Text>
              数据集 {meta.dataset.id} · 生成 {meta.llm_model}（{meta.prompt.version}） · 评分 {meta.quality_model}（{meta.quality_prompt_version}）
            </Typography.Text>
            <QualityRubricHelp />
          </Space>
        )}
      />
    </div>
  )
}
