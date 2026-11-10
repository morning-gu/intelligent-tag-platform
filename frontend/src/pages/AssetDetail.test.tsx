// 内容资产详情测试（v2）：mock apiFetch 按路径分发（未命中即抛错），覆盖：
// 三路并取渲染（视频 src/识别徽标/摘要/场景时间轴/挂载表新列）、URL query
// ?scene=&t= 挂载定位（seek 赋值 + 场景选中高亮）、时间轴点击 seek、未识别
// （content 404）非致命降级——警示 + 播放器/挂载表照常、内容加载失败非致命、
// 整页加载失败。
// v1→v2 语义映射：挂载表瘦身（评分/相关度/审核状态列与行展开证据卡删除，
// 证据查看走审核中心与任务详情）；origin 词表 generated/catalog → closed/
// open（历史挂载无对应 pre 行时为 null → "—"）；v1 "暂无识别上下文" 空态
// → content 404 的 "未完成内容识别" 警示降级。
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import AssetDetail from './AssetDetail'
import { ApiError, apiFetch } from '../api/client'
import type { AssetDetailData, AssetMount, ContentContext } from '../api/assets'

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
  updated_at: '2026-10-31T09:00:00',
  recognized: true,
}

const SCENES = [
  { scene_id: 's1', time_range: { start_ms: 0, end_ms: 60_000 }, description: '日常训练',
    visual_features: [], dialogues: [], entities: [], events: [] },
  { scene_id: 's2', time_range: { start_ms: 60_000, end_ms: 120_000 }, description: '主角争执推搡',
    visual_features: [], dialogues: [], entities: [], events: [] },
  { scene_id: 's3', time_range: { start_ms: 120_000, end_ms: 180_000 }, description: '离别远行',
    visual_features: [], dialogues: [], entities: [], events: [] },
]

const CONTEXT: ContentContext = {
  schema_version: '1.0',
  asset: {
    asset_id: 'a1', asset_type: 'movie', title: '星际穿越', duration_ms: 180_000,
    video_uri: 'movies/interstellar.mp4', parent_id: null,
  },
  content: { summary: '主角在太空遇难', scenes: SCENES },
  metadata: { source: 'test', model_version: 'vision-v3', recognition_status: 'completed' },
}

const TAGS: AssetMount[] = [
  {
    id: 1,
    tag_id: 'tag-quarrel',
    tag_name: '主角争执',
    origin: 'closed',
    created_at: '2026-11-01T10:00:00',
  },
  {
    id: 2,
    tag_id: 'tag-war',
    tag_name: '枪战',
    origin: 'open',
    created_at: null,
  },
  {
    id: 3,
    tag_id: 'tag-legacy',
    tag_name: '历史挂载',
    origin: null,
    created_at: '2026-10-20T08:00:00',
  },
]

interface MockOptions {
  detail?: AssetDetailData
  content?: ContentContext | null
  tags?: AssetMount[]
  /** content 端点抛 ApiError(contentStatus)：404 = 未识别（降级空态），
   * 其他 = 加载失败（警示透出 contentFailMessage）。 */
  contentStatus?: number
  contentFailMessage?: string
}

/** 按路径分发的 apiFetch 桩（详情/内容/挂载标签三端点）。 */
function mockApis(options: MockOptions = {}) {
  mockedApiFetch.mockImplementation(async (path: string) => {
    if (path === '/api/v1/assets/a1') return options.detail ?? DETAIL
    if (path === '/api/v1/assets/a1/content') {
      if (options.contentStatus !== undefined) {
        throw new ApiError(options.contentStatus, options.contentFailMessage ?? '内容加载失败')
      }
      return options.content ?? CONTEXT
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
  it('三路并取渲染：视频/识别徽标/摘要/场景时间轴/挂载表新列', async () => {
    mockApis()
    const { container } = renderDetail()

    // 视频 src 直连视频流端点（Range 206 供拖动）
    expect(videoEl(container).src).toContain('/api/v1/assets/a1/video')
    expect(await screen.findByText('星际穿越')).toBeInTheDocument()
    // 识别状态徽标（recognized = true → 识别完成）
    expect(screen.getByText('识别完成')).toBeInTheDocument()
    // 摘要与三场景时间轴
    expect(screen.getByText('主角在太空遇难')).toBeInTheDocument()
    expect(screen.getByText('s1')).toBeInTheDocument()
    expect(screen.getByText('s2')).toBeInTheDocument()
    expect(screen.getByText('s3')).toBeInTheDocument()

    // 挂载表新列：标签名/来源/创建时间
    expect(screen.getByText('标签名')).toBeInTheDocument()
    expect(screen.getByText('来源')).toBeInTheDocument()
    expect(screen.getByText('创建时间')).toBeInTheDocument()
    // v1 的评分/相关度/审核状态列删除
    expect(screen.queryByText('置信度')).not.toBeInTheDocument()
    expect(screen.queryByText('相关度')).not.toBeInTheDocument()
    expect(screen.queryByText('状态')).not.toBeInTheDocument()

    // origin 词表 v2：closed → 闭环 / open → 开放 / null（历史挂载）→ "—"；
    // created_at 格式化（T → 空格截秒）与 null → "—"
    const rowA = screen.getByText('主角争执').closest('tr')!
    expect(rowA).toHaveTextContent('闭环')
    expect(rowA).toHaveTextContent('2026-11-01 10:00:00')
    expect(rowA.textContent).not.toContain('—')
    const rowB = screen.getByText('枪战').closest('tr')!
    expect(rowB).toHaveTextContent('开放')
    expect(within(rowB).getByText('—')).toBeInTheDocument()
    const rowC = screen.getByText('历史挂载').closest('tr')!
    expect(within(rowC).getByText('—')).toBeInTheDocument()
    expect(rowC).toHaveTextContent('2026-10-20 08:00:00')

    // 行展开（证据卡）随挂载表瘦身删除：无展开钮——证据查看走审核中心
    expect(document.querySelector('.ant-table-row-expand-icon')).toBeNull()
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

  it('未识别（content 404）非致命降级：警示"未完成内容识别"，播放器与挂载表照常', async () => {
    mockApis({ detail: { ...DETAIL, recognized: false }, contentStatus: 404 })
    const { container } = renderDetail()

    expect(await screen.findByText('未完成内容识别')).toBeInTheDocument()
    // 头部识别徽标同步未识别
    expect(screen.getByText('未识别')).toBeInTheDocument()
    // 播放器与挂载表照常渲染（降级不波及主功能）
    expect(videoEl(container).src).toContain('/api/v1/assets/a1/video')
    expect(await screen.findByText('主角争执')).toBeInTheDocument()
    // 时间轴不渲染（无场景数据）
    expect(screen.queryByTestId('scene-timeline')).not.toBeInTheDocument()
  })

  it('内容加载失败（非 404）非致命：警示透出错误文案，挂载表仍可用', async () => {
    mockApis({ contentStatus: 500, contentFailMessage: '内容服务不可用' })
    const { container } = renderDetail()

    expect(await screen.findByText('场景数据加载失败')).toBeInTheDocument()
    expect(screen.getByText('内容服务不可用')).toBeInTheDocument()
    // 挂载表与播放器不受影响
    expect(await screen.findByText('主角争执')).toBeInTheDocument()
    expect(videoEl(container).src).toContain('/api/v1/assets/a1/video')
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
})
