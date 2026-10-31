// 证据卡（Task 10）：审核行的证据明细列表——场景号 + 时间段（毫秒 →
// mm:ss）+ 描述。点击任一项跳内容资产详情并定位到该场景（scene + 起始
// 毫秒入参），定位参数由内容详情页消费（F4 阶段实现）。
import { Button, Tag, Typography } from 'antd'
import { useNavigate } from 'react-router-dom'
import { formatTimestamp, type ReviewEvidence } from '../api/review'

interface EvidenceCardProps {
  /** 所属资产 id（导航目标 /assets/{asset_id} 的路径参数）。 */
  assetId: string
  /** 该标签行的证据明细（后端按行内 evidence_json 解析返回）。 */
  evidence: ReviewEvidence[]
}

export default function EvidenceCard({ assetId, evidence }: EvidenceCardProps) {
  const navigate = useNavigate()

  if (evidence.length === 0) {
    return <Typography.Text type="secondary">暂无证据</Typography.Text>
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      {evidence.map((ev) => (
        <Button
          key={`${ev.scene_id}-${ev.start_ms}`}
          size="small"
          style={{ justifyContent: 'flex-start', height: 'auto', paddingInline: 8 }}
          onClick={() => navigate(`/assets/${assetId}?scene=${ev.scene_id}&t=${ev.start_ms}`)}
        >
          <Tag style={{ marginInlineEnd: 8 }}>{ev.scene_id}</Tag>
          <Typography.Text type="secondary">
            {`${formatTimestamp(ev.start_ms)} - ${formatTimestamp(ev.end_ms)}`}
          </Typography.Text>
          <Typography.Text style={{ marginInlineStart: 8 }}>{ev.description}</Typography.Text>
        </Button>
      ))}
    </div>
  )
}
