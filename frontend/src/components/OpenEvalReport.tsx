import { Alert, Button, Collapse, Descriptions, Space, Table, Tag, Typography } from 'antd'
import type { TableProps } from 'antd'
import { openEvalApi, type OpenEvalLabel, type OpenEvalReport } from '../api/eval'
import type { QualityScore, QualityStatus } from '../api/tasks'
import QualityRubricHelp from './QualityRubricHelp'
import { QualityMetricsDetail } from './TagQualityEvaluation'

const filmStatuses: Partial<Record<QualityStatus, string>> = {
  completed: '评估完成',
  not_needed: '未产出标签',
  partial: '部分标签已评分',
  failed: '评分失败',
  generation_failed: '生成失败',
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
  const { meta, films } = report
  return (
    <Space orientation="vertical" size="middle" style={{ display: 'flex' }}>
      {report.incomplete && <Alert type="warning" showIcon title="部分结果" description="汇总覆盖已产出的标签。请结合影片状态及评分覆盖率查看。" />}
      {(meta.mock_llm || meta.mock_embedder) && <Alert type="info" showIcon title="本次运行使用了模拟模型" />}
      <Space wrap>
        <Button href={openEvalApi.fileUrl(runId, 'report.json')}>下载报告</Button>
        <Button href={openEvalApi.fileUrl(runId, 'raw_results.json')}>下载逐片结果</Button>
        <QualityRubricHelp />
      </Space>
      <Descriptions size="small" column={{ xs: 1, sm: 2 }} items={[
        { key: 'dataset', label: '数据集', children: meta.dataset.id },
        { key: 'movies', label: '已处理影片', children: `${meta.done} / ${meta.total} 部` },
        { key: 'generation', label: '生成模型', children: `${meta.llm_model} · ${meta.prompt.version}` },
        { key: 'quality', label: '评估模型', children: `${meta.quality_model} · ${meta.quality_prompt_version}` },
      ]} />
      <Space wrap>
        {Object.entries(report.film_counts).map(([status, count]) => (
          <Tag key={status}>{filmStatuses[status as QualityStatus] ?? status} {count} 部</Tag>
        ))}
      </Space>
      <QualityMetricsDetail metrics={report.metrics} />
      {films.length > 0 && (
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
              {film.metrics && <QualityMetricsDetail metrics={film.metrics} />}
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
      )}
    </Space>
  )
}
