// 场景时间轴（Task 11）：横向条带，每场景宽度 ∝ 场景时长（占 duration_ms
// 百分比）。三种视觉态：基色（未涉及）、证据高亮（标签 evidence 涉及的
// 场景集合——承诺二①"标签反向高亮"，金色）、选中（当前定位场景，主蓝，
// 优先级高于证据高亮）。hover 经 Tooltip 显示场景描述，点击回调 onSceneClick
// （父组件负责 seek 到场景起始毫秒）。
import { Tooltip, Typography } from 'antd'
import type { AssetScene } from '../api/assets'

interface SceneTimelineProps {
  /** 场景列表（来自内容 JSON，按时间顺序）。 */
  scenes: AssetScene[]
  /** 总时长毫秒（宽度百分比基准；<= 0 时退化为等宽分布）。 */
  durationMs: number
  /** 当前选中场景 id（场景点击/证据定位写入）。 */
  selectedSceneId?: string | null
  /** 标签 evidence 涉及的场景 id 集合（金色反向高亮）。 */
  highlightSceneIds?: string[]
  /** 场景分段点击回调（父组件 seek + 选中）。 */
  onSceneClick?: (scene: AssetScene) => void
}

export default function SceneTimeline({
  scenes,
  durationMs,
  selectedSceneId = null,
  highlightSceneIds = [],
  onSceneClick,
}: SceneTimelineProps) {
  if (scenes.length === 0) {
    return <Typography.Text type="secondary">无场景数据</Typography.Text>
  }

  const highlighted = new Set(highlightSceneIds)
  // 等宽兜底：总时长缺失/为 0 时按场景数均分（仍可点击与高亮）
  const fallbackPct = 100 / scenes.length

  return (
    <div className="scene-timeline" data-testid="scene-timeline">
      {scenes.map((scene) => {
        const span = scene.time_range.end_ms - scene.time_range.start_ms
        const pct = durationMs > 0 ? (span / durationMs) * 100 : fallbackPct
        const isSelected = scene.scene_id === selectedSceneId
        // 选中态优先于证据高亮（一个分段只呈现一种主色）
        const className = isSelected
          ? 'scene-timeline-segment scene-timeline-selected'
          : highlighted.has(scene.scene_id)
            ? 'scene-timeline-segment scene-timeline-highlighted'
            : 'scene-timeline-segment'
        return (
          <Tooltip key={scene.scene_id} title={`${scene.scene_id}：${scene.description}`}>
            <button
              type="button"
              className={className}
              data-scene-id={scene.scene_id}
              style={{ width: `${pct}%` }}
              onClick={() => onSceneClick?.(scene)}
            >
              {scene.scene_id}
            </button>
          </Tooltip>
        )
      })}
    </div>
  )
}
