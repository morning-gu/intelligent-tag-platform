// 证据卡（Task 10）：审核行的证据明细列表——场景号 + 时间段（毫秒 →
// mm:ss）+ 描述。点击任一项跳内容资产详情并定位到该场景（scene + 起始
// 毫秒入参）；Task 11 起支持宿主页面传入 onEvidenceClick 原位消费（内容
// 详情页改走播放器 seek + 场景高亮，不再跳转）。
import { Button, Tag, Typography } from 'antd'
import { useNavigate } from 'react-router-dom'
import { formatTimestamp, type ReviewEvidence } from '../api/review'

interface EvidenceCardProps {
  /** 所属资产 id（导航目标 /assets/{asset_id} 的路径参数）。 */
  assetId: string
  /** 该标签行的证据明细（后端按行内 evidence_json 解析返回）。 */
  evidence: ReviewEvidence[]
  /** 提供时点击证据不跳转，改回调给宿主页面原位定位（seek + 高亮）。 */
  onEvidenceClick?: (ev: ReviewEvidence) => void
}

export default function EvidenceCard({ assetId, evidence, onEvidenceClick }: EvidenceCardProps) {
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
          onClick={() => {
            if (onEvidenceClick !== undefined) {
              onEvidenceClick(ev)
            } else {
              navigate(`/assets/${assetId}?scene=${ev.scene_id}&t=${ev.start_ms}`)
            }
          }}
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
