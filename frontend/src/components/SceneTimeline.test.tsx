// 场景时间轴测试（Task 11）：比例宽度（占 duration_ms 百分比）、点击回调
// 携带场景对象、选中/证据高亮类名（选中优先）、hover Tooltip 描述、空态与
// 总时长缺失时的等宽兜底。
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import SceneTimeline from './SceneTimeline'
import type { AssetScene } from '../api/assets'

function makeScene(sceneId: string, startMs: number, endMs: number): AssetScene {
  return {
    scene_id: sceneId,
    start_ms: startMs,
    end_ms: endMs,
    description: `${sceneId} 的描述文本`,
    virtual_time: null,
    location: null,
    character_ids: [],
    events: [],
    emotion: [],
    dialogues: [],
    audio_events: [],
  }
}

const SCENES: AssetScene[] = [
  makeScene('s1', 0, 1000),
  makeScene('s2', 1000, 3000),
]

describe('SceneTimeline 场景时间轴', () => {
  it('每场景宽度按场景时长占 duration_ms 的百分比渲染（25% / 50%）', () => {
    render(<SceneTimeline scenes={SCENES} durationMs={4000} />)

    const s1 = screen.getByText('s1') as HTMLElement
    const s2 = screen.getByText('s2') as HTMLElement
    expect(s1.style.width).toBe('25%')
    expect(s2.style.width).toBe('50%')
  })

  it('点击分段回调 onSceneClick 并携带完整场景对象', () => {
    const onSceneClick = vi.fn()
    render(<SceneTimeline scenes={SCENES} durationMs={4000} onSceneClick={onSceneClick} />)

    fireEvent.click(screen.getByText('s2'))

    expect(onSceneClick).toHaveBeenCalledTimes(1)
    expect(onSceneClick).toHaveBeenCalledWith(SCENES[1])
  })

  it('选中场景带 selected 类，其余不带', () => {
    render(<SceneTimeline scenes={SCENES} durationMs={4000} selectedSceneId="s2" />)

    expect(screen.getByText('s2')).toHaveClass('scene-timeline-selected')
    expect(screen.getByText('s1')).not.toHaveClass('scene-timeline-selected')
  })

  it('证据高亮集合中的场景带 highlighted 类（标签反向高亮）', () => {
    render(<SceneTimeline scenes={SCENES} durationMs={4000} highlightSceneIds={['s1']} />)

    expect(screen.getByText('s1')).toHaveClass('scene-timeline-highlighted')
    expect(screen.getByText('s2')).not.toHaveClass('scene-timeline-highlighted')
  })

  it('同场景既选中又涉及证据时：只呈现选中态（选中优先）', () => {
    render(
      <SceneTimeline
        scenes={SCENES}
        durationMs={4000}
        selectedSceneId="s1"
        highlightSceneIds={['s1', 's2']}
      />,
    )

    expect(screen.getByText('s1')).toHaveClass('scene-timeline-selected')
    expect(screen.getByText('s1')).not.toHaveClass('scene-timeline-highlighted')
    // 未选中的证据场景仍保持金色高亮
    expect(screen.getByText('s2')).toHaveClass('scene-timeline-highlighted')
  })

  it('hover 分段：Tooltip 显示场景 id 与描述', async () => {
    render(<SceneTimeline scenes={SCENES} durationMs={4000} />)

    fireEvent.mouseEnter(screen.getByText('s2'))

    expect(await screen.findByText('s2：s2 的描述文本')).toBeInTheDocument()
  })

  it('场景列表为空：展示空态提示', () => {
    render(<SceneTimeline scenes={[]} durationMs={4000} />)

    expect(screen.getByText('无场景数据')).toBeInTheDocument()
  })

  it('durationMs 为 0（总时长缺失）：退化为等宽分布', () => {
    render(<SceneTimeline scenes={SCENES} durationMs={0} />)

    expect((screen.getByText('s1') as HTMLElement).style.width).toBe('50%')
    expect((screen.getByText('s2') as HTMLElement).style.width).toBe('50%')
  })
})
