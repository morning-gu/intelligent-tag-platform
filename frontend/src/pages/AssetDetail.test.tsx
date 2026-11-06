// 内容资产详情测试（Task 11）：mock apiFetch 按路径分发（未命中即抛错），
// 覆盖：详情渲染（视频 src/摘要/时间轴/标签行）、URL query ?scene=&t= 挂载
// 定位（seek 赋值 + 场景选中高亮）、时间轴点击 seek、标签证据点击 seek +
// 高亮（EvidenceCard 原位消费）、整页加载失败、上下文失败非致命、无上下文
// 空态。
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import AssetDetail from './AssetDetail'
import { apiFetch } from '../api/client'
import type { AssetDetailData, AssetTagRow, ContentContext } from '../api/assets'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, apiFetch: vi.fn() }
})

const mockedApiFetch = vi.mocked(apiFetch)

// ---------- 夹具（与后端 AssetDetail / ContentJSON / AssetTagOut 契约同形） ----------

const DETAIL: AssetDetailData = {
  asset_id: 'a1',
  asset_type: 'movie',
  parent_id: null,
  title: '星际穿越',
  duration_ms: 180_000,
  video_uri: 'movies/interstellar.mp4',
  has_content: true,
  created_at: '2026-10-30T08:30:00',
  updated_at: '2026-11-02T09:10:00',
  contexts: [
    {
      context_id: 'c1',
      model_version: 'vision-v3',
      recognition_status: 'completed',
      created_at: '2026-10-30T08:30:00',
    },
  ],
}

const SCENES = [
  { scene_id: 's1', start_ms: 0, end_ms: 60_000, description: '日常训练',
    virtual_time: null, location: null, character_ids: [], events: [],
    emotion: [], dialogues: [], audio_events: [] },
  { scene_id: 's2', start_ms: 60_000, end_ms: 120_000, description: '主角争执推搡',
    virtual_time: null, location: null, character_ids: [], events: [],
    emotion: [], dialogues: [], audio_events: [] },
  { scene_id: 's3', start_ms: 120_000, end_ms: 180_000, description: '离别远行',
    virtual_time: null, location: null, character_ids: [], events: [],
    emotion: [], dialogues: [], audio_events: [] },
]

const CONTEXT: ContentContext = {
  schema_version: '1.0',
  asset: {
    asset_id: 'a1', asset_type: 'movie', title: '星际穿越', duration_ms: 180_000,
    video_uri: 'movies/interstellar.mp4', language: 'zh-CN',
  },
  content: { description: '主角在太空遇难', characters: [], scenes: SCENES },
}

const TAGS: AssetTagRow[] = [
  {
    id: 1,
    tag_name: '主角争执',
    origin: 'generated',
    relevance_score: 0.8,
    confidence: 0.42,
    review_status: 'pending',
    level: 'scene',
    evidence: [{ scene_id: 's2', start_ms: 65_000, end_ms: 71_000, description: '主角争执推搡' }],
    tag_id: null,
    candidate_name: '主角争执',
  },
  {
    id: 2,
    tag_name: '枪战',
    origin: 'catalog',
    relevance_score: null,
    confidence: null,
    review_status: 'approved',
    level: 'film',
    evidence: [],
    tag_id: 'tag-war',
    candidate_name: null,
  },
]

interface MockOptions {
  detail?: AssetDetailData
  context?: ContentContext | null
  tags?: AssetTagRow[]
  contextFail?: boolean
}

/** 按路径分发的 apiFetch 桩（详情/上下文/标签三端点）。 */
function mockApis(options: MockOptions = {}) {
  mockedApiFetch.mockImplementation(async (path: string) => {
    if (path === '/api/v1/assets/a1') return options.detail ?? DETAIL
    if (path === '/api/v1/assets/a1/contexts/c1') {
      if (options.contextFail) throw new Error('上下文库不可用')
      return options.context ?? CONTEXT
    }
    if (path === '/api/v1/assets/a1/tags') return options.tags ?? TAGS
    throw new Error(`未预期的请求：${path}`)
  })
}

function videoEl(container: HTMLElement): HTMLVideoElement {
  const el = container.querySelector('video')
  expect(el).not.toBeNull()
  return el as HTMLVideoElement
}

function renderDetail(initialEntry = '/assets/a1') {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/assets/:assetId" element={<AssetDetail />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  mockedApiFetch.mockReset()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('AssetDetail 内容资产详情', () => {
  it('渲染视频/标题摘要/场景时间轴/标签行', async () => {
    mockApis()
    const { container } = renderDetail()

    // 视频 src 直连视频流端点（Range 206 供拖动）
    expect(videoEl(container).src).toContain('/api/v1/assets/a1/video')
    expect(await screen.findByText('星际穿越')).toBeInTheDocument()
    expect(screen.getByText('主角在太空遇难')).toBeInTheDocument()
    // 时间轴三场景 + 标签行（来源中文/状态中文/null 兜底）
    expect(screen.getByText('s1')).toBeInTheDocument()
    expect(screen.getByText('s2')).toBeInTheDocument()
    expect(screen.getByText('s3')).toBeInTheDocument()
    const tagRow = screen.getByText('主角争执').closest('tr')!
    expect(tagRow).toHaveTextContent('生成')
    expect(tagRow).toHaveTextContent('待审核')
    expect(tagRow).toHaveTextContent('42%')
    // 层级列：生成行 scene、目录行 film
    expect(within(tagRow).getByText('场景')).toBeInTheDocument()
    const rowB = screen.getByText('枪战').closest('tr')!
    expect(rowB).toHaveTextContent('目录')
    expect(within(rowB).getByText('影片')).toBeInTheDocument()
    expect(within(rowB).getAllByText('暂无')).toHaveLength(2)
  })

  it('场景资产：卡片标题截断预览，正文展示完整描述', async () => {
    const longTitle =
      'The Dude 抵达 The Big Lebowski 的豪宅，Brandt 先带他参观了展示 Lebowski 慈善成就的奖项。' +
      '随后在书房，坐轮椅的 The Big Lebowski 态度强硬，拒绝为被毁的地毯负责。'
    mockApis({ detail: { ...DETAIL, asset_type: 'scene', title: longTitle } })
    renderDetail()

    const description = await screen.findByTestId('asset-description')
    expect(description).toHaveTextContent(longTitle)
    // 全文只出现一次（卡片标题是截断预览，不重复渲染全文）
    expect(screen.getAllByText(longTitle)).toHaveLength(1)
  })

  it('URL query ?scene=&t=：挂载即 seek 到 t 毫秒并选中场景', async () => {
    mockApis()
    const { container } = renderDetail('/assets/a1?scene=s2&t=65000')

    await screen.findByText('星际穿越')

    // 65000ms → currentTime 65 秒（播放器不依赖详情加载，挂载即可定位）
    expect(videoEl(container).currentTime).toBe(65)
    await waitFor(() => expect(screen.getByText('s2')).toHaveClass('scene-timeline-selected'))
  })

  it('URL query 定位后点击其他场景：选中随点击切换（点击覆盖 query 派生值）', async () => {
    mockApis()
    const { container } = renderDetail('/assets/a1?scene=s2&t=65000')
    await screen.findByText('s1')

    fireEvent.click(screen.getByText('s3'))

    expect(videoEl(container).currentTime).toBe(120)
    const timeline = screen.getByTestId('scene-timeline')
    expect(within(timeline).getByText('s3')).toHaveClass('scene-timeline-selected')
    expect(within(timeline).getByText('s2')).not.toHaveClass('scene-timeline-selected')
  })

  it('点击场景分段：seek 到场景起始毫秒 + 选中高亮', async () => {
    mockApis()
    const { container } = renderDetail()
    await screen.findByText('s1')

    fireEvent.click(screen.getByText('s3'))

    expect(videoEl(container).currentTime).toBe(120)
    expect(screen.getByText('s3')).toHaveClass('scene-timeline-selected')
    expect(screen.getByText('s1')).not.toHaveClass('scene-timeline-selected')
  })

  it('标签证据涉及的场景常驻金色反向高亮', async () => {
    mockApis()
    renderDetail()
    await screen.findByText('s1')

    expect(screen.getByText('s2')).toHaveClass('scene-timeline-highlighted')
    expect(screen.getByText('s1')).not.toHaveClass('scene-timeline-highlighted')
  })

  it('展开标签行点击证据：seek 到证据 start_ms + 高亮对应场景（原位不跳转）', async () => {
    mockApis()
    const { container } = renderDetail()
    await screen.findByText('主角争执')

    // 仅首行有证据可展开（rowExpandable=false 的行无展开钮）
    const expandIcon = document.querySelector('.ant-table-row-expand-icon')
    expect(expandIcon).not.toBeNull()
    fireEvent.click(expandIcon as Element)

    // 证据按钮可访问名含时间与描述（时间轴分段无时间文本，不歧义）
    fireEvent.click(await screen.findByRole('button', { name: /主角争执推搡/ }))

    // 证据卡内也有场景号 Tag 's2'——断言限定时间轴容器内
    expect(videoEl(container).currentTime).toBe(65)
    const timeline = screen.getByTestId('scene-timeline')
    expect(within(timeline).getByText('s2')).toHaveClass('scene-timeline-selected')
  })

  it('详情/标签加载失败：整页错误提示（透出错误文案）', async () => {
    mockedApiFetch.mockImplementation(async (path: string) => {
      if (path === '/api/v1/assets/a1/tags') return TAGS
      throw new Error('资产不存在：a1')
    })
    renderDetail()

    expect(await screen.findByText('资产详情加载失败')).toBeInTheDocument()
    expect(screen.getByText('资产不存在：a1')).toBeInTheDocument()
  })

  it('上下文加载失败非致命：时间轴警示但标签列表仍可用', async () => {
    mockApis({ contextFail: true })
    renderDetail()

    expect(await screen.findByText('场景数据加载失败')).toBeInTheDocument()
    expect(screen.getByText('上下文库不可用')).toBeInTheDocument()
    // 标签列表不受影响
    expect(await screen.findByText('主角争执')).toBeInTheDocument()
    expect(screen.queryByText('无场景数据')).not.toBeInTheDocument()
  })

  it('资产无上下文：时间轴空态提示"暂无识别上下文"', async () => {
    mockApis({ detail: { ...DETAIL, contexts: [] } })
    renderDetail()

    expect(await screen.findByText('暂无识别上下文')).toBeInTheDocument()
  })
})
