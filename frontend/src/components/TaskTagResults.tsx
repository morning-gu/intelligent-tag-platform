
import { Button, Descriptions, Drawer, Table, Tabs, Tag, Tooltip, Typography } from 'antd'
import type { TableProps } from 'antd'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { GenerationTaskResult, QualityLabel, QualityScore } from '../api/tasks'
import EvidenceCard from './EvidenceCard'
import './TaskTagResults.css'

const { Text } = Typography
const targetLabels: Record<string, string> = { content: '整片', scene: '单场景', character: '人物' }
const reviewLabels = {
  pending: { label: '待审核', color: 'gold' },
  approved: { label: '已通过', color: 'green' },
  rejected: { label: '已拒绝', color: 'red' },
}

interface ResultRow {
  id: number
  name: string
  result?: GenerationTaskResult
  label?: QualityLabel
}

type DetailTab = 'quality' | 'evidence'

function Score({ score, maxScore, empty = '未完成' }: {
  score?: QualityScore | null
  maxScore: number
  empty?: string
}) {
  if (!score || score.score === null) {
    return <Text type="secondary">{score ? '无法判断' : empty}</Text>
  }
  const color = score.score === maxScore ? 'success' : score.score === 0 ? 'error' : 'warning'
  return <Tag color={color}>{score.score} 分</Tag>
}

function Directory({ path }: { path?: string | null }) {
  return path ? (
    <Tooltip title={path} placement="topLeft">
      <span className="task-tag-ellipsis">{path}</span>
    </Tooltip>
  ) : <Text type="secondary">未归属</Text>
}

function QualityDetails({ label, assetId, maxScore }: { label: QualityLabel; assetId: string; maxScore: number }) {
  const score = label.score
  return (
    <>
      {score && (
        <section className="task-tag-reasons" aria-label="各项评分理由">
          {([
            ['内容相关性', score.relevance],
            ['语义准确性', score.semantics],
            ['标签价值', score.value],
          ] as const).map(([title, result]) => (
            <div className="task-tag-reason" key={title}>
              <div><Text strong>{title}</Text><Score score={result} maxScore={maxScore} empty="未评估" /></div>
              {result && <p>{result.reason}</p>}
            </div>
          ))}
        </section>
      )}
      {!score && <Text type="secondary">评分未完成</Text>}
      {!!score?.evidence.length && (
        <section className="task-tag-drawer-section">
          <Text strong>评分引用</Text>
          <ul className="task-tag-citations">
            {score.evidence.map(({ scene_id, start_ms, description }, index) => (
              <li key={index}>
                {scene_id && start_ms !== null && (
                  <Link to={`/assets/${assetId}?scene=${encodeURIComponent(scene_id)}&t=${start_ms}`}>{scene_id}</Link>
                )}
                {description}
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  )
}

export default function TaskTagResults({ results, labels = [], assetId, maxScore = 2 }: {
  results: GenerationTaskResult[]
  labels?: QualityLabel[]
  assetId: string
  maxScore?: number
}) {
  const [selection, setSelection] = useState<{ id: number; tab: DetailTab } | null>(null)
  const labelsById = new Map(labels.map((label) => [label.id, label]))
  const resultIds = new Set(results.map((result) => result.id))
  // 评分使用生产时的快照。重跑可能清掉待审核行，仍可查看原快照的评分。
  const rows: ResultRow[] = [
    ...results.map((result) => ({
      id: result.id, name: result.tag_name, result, label: labelsById.get(result.id),
    })),
    ...labels.filter((label) => !resultIds.has(label.id))
      .map((label) => ({ id: label.id, name: label.name, label })),
  ]
  const selected = rows.find((row) => row.id === selection?.id)
  const evidence = selected?.result?.evidence ?? []

  function openDetail(row: ResultRow, tab: DetailTab = row.label ? 'quality' : 'evidence') {
    setSelection({ id: row.id, tab })
  }

  const columns: NonNullable<TableProps<ResultRow>['columns']> = [
    {
      title: '标签', key: 'name', width: 155, fixed: 'left',
      render: (_, row) => {
        const first = row.result?.evidence[0]
        return first ? (
          <Link className="task-tag-ellipsis" title={row.name}
            to={`/assets/${assetId}?scene=${encodeURIComponent(first.scene_id)}&t=${first.start_ms}`}>{row.name}</Link>
        ) : <Text className="task-tag-ellipsis" title={row.name}>{row.name}</Text>
      },
    },
    ...(labels.length ? ([
      ['内容相关性', 'relevance'],
      ['语义准确性', 'semantics'],
      ['标签价值', 'value'],
    ] as const).map(([title, key]) => ({
      title, key, width: 90, align: 'center' as const,
      render: (_: unknown, row: ResultRow) => row.label
        ? <Score score={row.label.score?.[key]} maxScore={maxScore} empty={row.label.score ? '未评估' : '未完成'} />
        : <Text type="secondary">—</Text>,
    })) : []),
    {
      title: '明细', key: 'detail', width: 68,
      render: (_, row) => (
        <Button size="small" type="link" aria-label={`查看${row.name}详情`}
          onClick={() => openDetail(row)}>查看</Button>
      ),
    },
    {
      title: '目录', key: 'directory',
      render: (_, { result, label }) => (
        <Directory path={result ? result.dir_path ?? result.dir_name : label?.dir_path} />
      ),
    },
    {
      title: '粒度', key: 'target', width: 74,
      render: (_, { result }) => result
        ? <Tag color={result.target_type === 'content' ? 'default' : 'cyan'}>
            {targetLabels[result.target_type] ?? result.target_type}
          </Tag>
        : <Text type="secondary">—</Text>,
    },
    {
      title: '审核状态', key: 'status', width: 88,
      render: (_, { result }) => {
        if (!result) return <Tag>评分快照</Tag>
        const status = reviewLabels[result.review_status]
        return status ? <Tag color={status.color}>{status.label}</Tag> : result.review_status
      },
    },
    {
      title: <Tooltip title="开放生成时表示与库词的相似度">相关度</Tooltip>, key: 'relevance_score', width: 65, align: 'right',
      render: (_, { result }) => result ? result.relevance_score?.toFixed(2) ?? '暂无' : '—',
    },
    {
      title: '置信度', key: 'confidence', width: 65, align: 'right',
      render: (_, { result }) => result ? result.confidence?.toFixed(2) ?? '暂无' : '—',
    },
    {
      title: '场景证据', key: 'evidence', width: 84,
      render: (_, row) => {
        const count = row.result?.evidence.length ?? 0
        return count > 0 ? (
          <Button size="small" type="link" aria-label={`查看${row.name}的 ${count} 段场景证据`}
            onClick={() => openDetail(row, 'evidence')}>{count} 段</Button>
        ) : <Text type="secondary">—</Text>
      },
    },
  ]

  return (
    <div className="task-tag-results">
      <Table<ResultRow> rowKey="id" size="small" tableLayout="fixed" columns={columns}
        dataSource={rows} scroll={{ x: labels.length ? 1050 : 770 }}
        locale={{ emptyText: '暂无标签产出' }}
        pagination={rows.length > 15 ? { pageSize: 15, showSizeChanger: false } : false}
        rowClassName={(row) => row.id === selected?.id ? 'task-tag-selected' : ''} />
      <Drawer
        title={selected?.name}
        open={selected !== undefined}
        mask={false}
        size="min(500px, 100vw)"
        onClose={() => setSelection(null)}
      >
        {selected && (
          <>
            <Descriptions size="small" column={1} items={[
              { key: 'directory', label: '目录', children: selected.result?.dir_path ??
                selected.result?.dir_name ?? selected.label?.dir_path ?? '未归属' },
              ...(selected.result ? [{
                key: 'origin', label: '来源', children: selected.result.origin === 'open' ? '开放' : '闭环',
              }] : []),
              ...(selected.label ? [
                { key: 'disposition', label: '生产分流', children: selected.label.disposition === 'mounted'
                  ? '直接挂载' : '进入审核' },
                ...(selected.label.name !== selected.name ? [{
                  key: 'snapshot', label: '评分快照名称', children: selected.label.name,
                }] : []),
                { key: 'desc', label: '评分快照释义', children: selected.label.desc || '暂无释义' },
              ] : []),
            ]} />
            <Tabs
              activeKey={selection?.tab}
              onChange={(tab) => setSelection({ id: selected.id, tab: tab === 'quality' ? 'quality' : 'evidence' })}
              items={[
                ...(selected.label ? [{
                  key: 'quality', label: '评分理由',
                  children: <QualityDetails label={selected.label} assetId={assetId} maxScore={maxScore} />,
                }] : []),
                {
                  key: 'evidence', label: `场景证据（${evidence.length}）`,
                  children: <EvidenceCard assetId={assetId} evidence={evidence} />,
                },
              ]}
            />
          </>
        )}
      </Drawer>
    </div>
  )
}
