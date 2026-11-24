import { Button, Descriptions, Drawer, Table, Tag, Tooltip, Typography } from 'antd'
import type { TableProps } from 'antd'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { GenerationTaskResult } from '../api/tasks'
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
  result: GenerationTaskResult
}

function Directory({ path }: { path?: string | null }) {
  return path ? (
    <Tooltip title={path} placement="topLeft">
      <span className="task-tag-ellipsis">{path}</span>
    </Tooltip>
  ) : <Text type="secondary">未归属</Text>
}

export default function TaskTagResults({ results, assetId }: {
  results: GenerationTaskResult[]
  assetId: string
}) {
  const [selection, setSelection] = useState<number | null>(null)
  const rows: ResultRow[] = results.map((result) => ({
    id: result.id, name: result.tag_name, result,
  }))
  const selected = rows.find((row) => row.id === selection)
  const evidence = selected?.result.evidence ?? []

  const columns: NonNullable<TableProps<ResultRow>['columns']> = [
    {
      title: '标签', key: 'name', width: 155, fixed: 'left',
      render: (_, row) => {
        const first = row.result.evidence[0]
        return first ? (
          <Link className="task-tag-ellipsis" title={row.name}
            to={`/assets/${assetId}?scene=${encodeURIComponent(first.scene_id)}&t=${first.start_ms}`}>{row.name}</Link>
        ) : <Text className="task-tag-ellipsis" title={row.name}>{row.name}</Text>
      },
    },
    {
      title: '明细', key: 'detail', width: 68,
      render: (_, row) => (
        <Button size="small" type="link" aria-label={`查看${row.name}详情`}
          onClick={() => setSelection(row.id)}>查看</Button>
      ),
    },
    {
      title: '目录', key: 'directory',
      render: (_, { result }) => <Directory path={result.dir_path ?? result.dir_name} />,
    },
    {
      title: '粒度', key: 'target', width: 74,
      render: (_, { result }) => (
        <Tag color={result.target_type === 'content' ? 'default' : 'cyan'}>
          {targetLabels[result.target_type] ?? result.target_type}
        </Tag>
      ),
    },
    {
      title: '审核状态', key: 'status', width: 88,
      render: (_, { result }) => {
        const status = reviewLabels[result.review_status]
        return status ? <Tag color={status.color}>{status.label}</Tag> : result.review_status
      },
    },
    {
      title: <Tooltip title="开放生成时表示与库词的相似度">相关度</Tooltip>, key: 'relevance_score', width: 65, align: 'right',
      render: (_, { result }) => result.relevance_score?.toFixed(2) ?? '暂无',
    },
    {
      title: '置信度', key: 'confidence', width: 65, align: 'right',
      render: (_, { result }) => result.confidence?.toFixed(2) ?? '暂无',
    },
    {
      title: '场景证据', key: 'evidence', width: 84,
      render: (_, row) => {
        const count = row.result.evidence.length
        return count > 0 ? (
          <Button size="small" type="link" aria-label={`查看${row.name}的 ${count} 段场景证据`}
            onClick={() => setSelection(row.id)}>{count} 段</Button>
        ) : <Text type="secondary">—</Text>
      },
    },
  ]

  return (
    <div className="task-tag-results">
      <Table<ResultRow> rowKey="id" size="small" tableLayout="fixed" columns={columns}
        dataSource={rows} scroll={{ x: 770 }}
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
              { key: 'directory', label: '目录', children: selected.result.dir_path ??
                selected.result.dir_name ?? '未归属' },
              { key: 'origin', label: '来源', children: selected.result.origin === 'open' ? '开放' : '闭环' },
            ]} />
            <section className="task-tag-drawer-section" aria-label="场景证据">
              <Text strong>{`场景证据（${evidence.length}）`}</Text>
              <EvidenceCard assetId={assetId} evidence={evidence} />
            </section>
          </>
        )}
      </Drawer>
    </div>
  )
}
