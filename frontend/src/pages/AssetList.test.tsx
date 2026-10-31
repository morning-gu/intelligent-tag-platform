// 内容资产列表测试（Task 11）：mock apiFetch 按路径分发，覆盖：列表渲染
// （类型/时长/视频/创建时间展示与 null 兜底）、类型筛选传参、标题搜索传参
// （提交触发而非输入即触发）、行点击进入详情路由。
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import AssetList from './AssetList'
import { apiFetch } from '../api/client'
import type { AssetSummary } from '../api/assets'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, apiFetch: vi.fn() }
})

const mockedApiFetch = vi.mocked(apiFetch)

// ---------- 夹具（与后端 AssetOut 契约同形） ----------

const ASSETS: AssetSummary[] = [
  {
    asset_id: 'a-movie',
    asset_type: 'movie',
    parent_id: null,
    title: '星际穿越',
    duration_ms: 10_800_000,
    video_uri: 'movies/interstellar.mp4',
    created_at: '2026-10-30T08:30:00',
  },
  {
    asset_id: 'a-scene',
    asset_type: 'scene',
    parent_id: 'a-movie',
    title: '星际穿越-场景2',
    duration_ms: 65_000,
    video_uri: null,
    created_at: null,
  },
]

/** 资产列表端点的请求路径（按调用顺序，断言筛选参数用）。 */
function assetsCallPaths(): string[] {
  return mockedApiFetch.mock.calls
    .map((call) => call[0])
    .filter((p): p is string => typeof p === 'string' && p.startsWith('/api/v1/assets'))
}

function renderAssetList() {
  return render(
    <MemoryRouter initialEntries={['/assets']}>
      <Routes>
        <Route path="/assets" element={<AssetList />} />
        {/* 详情路由以探针文本占位：行点击导航后可断言落地路径 */}
        <Route path="/assets/:assetId" element={<div>详情页占位</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  mockedApiFetch.mockReset()
  mockedApiFetch.mockImplementation(async (path: string) => {
    if (path === '/api/v1/assets' || path.startsWith('/api/v1/assets?')) return ASSETS
    throw new Error(`未预期的请求：${path}`)
  })
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('AssetList 内容资产列表', () => {
  it('渲染资产行：类型 Tag/时长 mm:ss/视频有无/创建时间与 null 兜底', async () => {
    renderAssetList()

    const row = (await screen.findByText('星际穿越')).closest('tr')!
    expect(row).toHaveTextContent('电影')
    // 10_800_000ms → 180:00（formatTimestamp 复用，分钟位不封顶）
    expect(row).toHaveTextContent('180:00')
    expect(row).toHaveTextContent('可播放')
    expect(row).toHaveTextContent('2026-10-30 08:30:00')
    const rowB = screen.getByText('星际穿越-场景2').closest('tr')!
    expect(rowB).toHaveTextContent('场景')
    expect(rowB).toHaveTextContent('01:05')
    expect(rowB).toHaveTextContent('无')
    expect(rowB).toHaveTextContent('—')
    // 默认无筛选：不带查询串
    expect(assetsCallPaths()).toEqual(['/api/v1/assets'])
  })

  it('类型筛选选电影：携带 asset_type=movie 重新拉取', async () => {
    renderAssetList()
    await screen.findByText('星际穿越')

    fireEvent.mouseDown(screen.getByRole('combobox', { name: '类型筛选' }))
    // antd v6 下拉存在隐藏的无障碍克隆（role=option、无点击处理器），
    // 真实可点选项带 title 属性——以 title 定位点击
    fireEvent.click(await screen.findByTitle('电影'))

    await waitFor(() => expect(assetsCallPaths()).toHaveLength(2))
    expect(assetsCallPaths()[1]).toBe('/api/v1/assets?asset_type=movie')
  })

  it('标题搜索：提交（按钮）触发带 q 重新拉取，输入本身不触发', async () => {
    renderAssetList()
    await screen.findByText('星际穿越')

    fireEvent.change(screen.getByPlaceholderText('标题搜索'), {
      target: { value: '星际' },
    })
    expect(assetsCallPaths()).toHaveLength(1) // 输入即触发则此处已两次

    fireEvent.click(screen.getByRole('button', { name: /搜\s*索/ }))

    await waitFor(() => expect(assetsCallPaths()).toHaveLength(2))
    expect(assetsCallPaths()[1]).toBe(`/api/v1/assets?q=${encodeURIComponent('星际')}`)
  })

  it('行点击进入详情路由 /assets/{asset_id}', async () => {
    renderAssetList()
    await screen.findByText('星际穿越')

    fireEvent.click(screen.getByText('星际穿越-场景2'))

    expect(await screen.findByText('详情页占位')).toBeInTheDocument()
  })

  it('列表加载失败：渲染错误提示（透出错误文案）', async () => {
    mockedApiFetch.mockImplementation(async () => {
      throw new Error('后端不可用')
    })
    renderAssetList()

    expect(await screen.findByText('资产列表加载失败')).toBeInTheDocument()
    expect(screen.getByText('后端不可用')).toBeInTheDocument()
  })
})
