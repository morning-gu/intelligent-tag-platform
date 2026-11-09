// 内容资产列表测试（Task 11）：mock apiFetch 按路径分发，覆盖：列表渲染
// （类型/时长/视频/创建时间展示与 null 兜底）、类型筛选传参、标题搜索传参
// （提交触发而非输入即触发）、行点击进入详情路由。
// 上传占位（阶段 4 任务 5，入口 A）：覆盖上传成功（POST FormData file 字段
// + 成功提示注明占位语义 + 列表刷新出现新行 + Modal 关闭）、未选文件禁用
// 确认、422/413 失败透出后端 detail 且不刷新列表；nginx 环境 413 响应为
// HTML（回落文案"请求失败（HTTP 413）"）时特判为 2GB 上限提示。
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import AssetList from './AssetList'
import { ApiError, apiFetch } from '../api/client'
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
    has_content: true,
    created_at: '2026-10-30T08:30:00',
    updated_at: '2026-10-30T08:30:00',
  },
  {
    asset_id: 'a-scene',
    asset_type: 'scene',
    parent_id: 'a-movie',
    title: '星际穿越-场景2',
    duration_ms: 65_000,
    video_uri: null,
    has_content: false,
    created_at: null,
    updated_at: null,
  },
]

/** 资产列表端点的请求路径（按调用顺序，断言筛选参数用）。 */
function assetsCallPaths(): string[] {
  return mockedApiFetch.mock.calls
    .map((call) => call[0])
    .filter((p): p is string => typeof p === 'string' && p.startsWith('/api/v1/assets'))
}

// ---------- 上传占位夹具（与后端 MediaUploadResponse / AssetOut 契约同形） ----------

const UPLOAD_RESULT = { asset_id: 'a-new', title: '新片', video_uri: 'C:\\data\\media\\u.mkv' }

const NEW_ASSET: AssetSummary = {
  asset_id: 'a-new',
  asset_type: 'movie',
  parent_id: null,
  title: '新片',
  duration_ms: null,
  video_uri: 'C:\\data\\media\\u.mkv',
  has_content: false,
  created_at: '2026-11-01T10:00:00',
  updated_at: '2026-11-01T10:00:00',
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

// ---------- 上传场景 mock 装配与查询辅助 ----------

/** 上传场景的 apiFetch 桩：资产列表（上传成功后含新行）+ /media/upload。 */
function mockUploadApis(options: { uploadError?: Error } = {}) {
  let assets: AssetSummary[] = ASSETS
  mockedApiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    if (method === 'POST' && path === '/api/v1/media/upload') {
      if (options.uploadError) throw options.uploadError
      assets = [NEW_ASSET, ...ASSETS] // 上传成功：后端新增资产行
      return UPLOAD_RESULT
    }
    if (path === '/api/v1/assets' || path.startsWith('/api/v1/assets?')) return assets
    throw new Error(`未预期的请求：${method} ${path}`)
  })
}

/** 上传端点的调用（未请求过返回 undefined）。 */
function uploadCall(): [string, RequestInit] | undefined {
  return mockedApiFetch.mock.calls.find(
    ([p, init]) =>
      p === '/api/v1/media/upload' && (init as RequestInit | undefined)?.method === 'POST',
  ) as [string, RequestInit] | undefined
}

/** 资产列表端点的调用次数（上传成功刷新后递增）。 */
function assetsCallCount(): number {
  return assetsCallPaths().length
}

/** 点"上传视频"打开 Modal 并选择视频文件（触发 antd Upload 的 beforeUpload
 * 拦截路径），返回所选 File 供载荷断言。 */
async function openUploadAndSelectFile(name = '新片.mkv') {
  fireEvent.click(screen.getByRole('button', { name: '上传视频' }))
  // Modal 已挂载（选择按钮出现）后才存在 input[type=file]
  expect(await screen.findByText('选择视频文件')).toBeInTheDocument()
  const input = document.querySelector('input[type="file"]') as HTMLInputElement
  expect(input).not.toBeNull()
  const file = new File(['fake-video-bytes'], name)
  fireEvent.change(input, { target: { files: [file] } })
  return file
}

/** 手动收尾 Modal 离场动画：jsdom 无真实 CSS 动画事件（且 rc-motion 事件
 * 探测到的监听名是 webkitAnimationEnd），离场 motion 永远不结束，Modal
 * 内容不会随 destroyOnHidden 卸载。等离场步进到 ACTIVE 步（监听挂载点，
 * class 出现 -leave-active）后补发 motion 结束事件使 motion 收尾
 * （afterClose → 摘除内容）；事件名覆盖 jsdom/标准两种探测结果。 */
async function finishModalLeaveMotion() {
  await waitFor(() =>
    expect(document.querySelector('.ant-modal')?.className).toContain('ant-zoom-leave-active'),
  )
  const modal = document.querySelector('.ant-modal') as HTMLElement
  for (const type of ['webkitAnimationEnd', 'animationend', 'transitionend']) {
    modal.dispatchEvent(new Event(type))
  }
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

describe('上传视频（入口 A 占位）', () => {
  it('上传成功：POST FormData（file 字段为所选文件）→ 成功提示注明占位语义 + 列表刷新出现新行 + Modal 关闭', async () => {
    mockUploadApis()
    renderAssetList()
    await screen.findByText('星际穿越')

    const file = await openUploadAndSelectFile()
    // 已选文件自绘展示（beforeUpload 返回 false 时 antd 内建列表不收文件）
    expect(screen.getByText('已选择：新片.mkv')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '开始上传' }))

    // POST 载荷：multipart FormData（client 不强设 Content-Type），file 字段即所选文件
    await waitFor(() => expect(uploadCall()).toBeDefined())
    const body = uploadCall()![1].body as FormData
    expect(body).toBeInstanceOf(FormData)
    expect(body.get('file')).toBe(file)

    // 成功提示注明占位语义（已登记但不做识别，指引入口 B）
    expect(
      await screen.findByText('已登记「新片」，未导入识别内容——请经入口 B 导入识别 JSON 后打标'),
    ).toBeInTheDocument()

    // Modal 关闭（补发离场动画事件后摘除内容）+ 列表刷新（第二次资产拉取）且新行出现
    await finishModalLeaveMotion()
    await waitFor(() => expect(screen.queryByText('选择视频文件')).not.toBeInTheDocument())
    await waitFor(() => expect(assetsCallCount()).toBe(2))
    expect(await screen.findByText('新片')).toBeInTheDocument()
  })

  it('未选文件时确认按钮禁用，取消按钮关闭 Modal', async () => {
    mockUploadApis()
    renderAssetList()
    await screen.findByText('星际穿越')

    fireEvent.click(screen.getByRole('button', { name: '上传视频' }))
    const ok = await screen.findByRole('button', { name: '开始上传' })
    expect(ok).toBeDisabled()

    // antd 两字按钮自动插空格（'取消' 渲染为 '取 消'），以宽松正则定位
    fireEvent.click(screen.getByRole('button', { name: /取\s*消/ }))
    await finishModalLeaveMotion()
    await waitFor(() => expect(screen.queryByText('选择视频文件')).not.toBeInTheDocument())
    expect(uploadCall()).toBeUndefined()
  })

  it('上传失败（422 非白名单后缀）：透出后端 detail，Modal 保留可重试，列表不刷新', async () => {
    mockUploadApis({ uploadError: new ApiError(422, '不支持的视频格式') })
    renderAssetList()
    await screen.findByText('星际穿越')

    await openUploadAndSelectFile()
    fireEvent.click(screen.getByRole('button', { name: '开始上传' }))

    expect(await screen.findByText('不支持的视频格式')).toBeInTheDocument()
    expect(assetsCallCount()).toBe(1) // 未刷新
    expect(screen.getByText('选择视频文件')).toBeInTheDocument() // Modal 仍在
    expect(screen.getByText('已选择：新片.mkv')).toBeInTheDocument() // 已选文件保留供重试
  })

  it('上传失败（413 超 2GB 上限）：透出后端 detail，列表不刷新', async () => {
    mockUploadApis({ uploadError: new ApiError(413, '文件大小超出上传上限') })
    renderAssetList()
    await screen.findByText('星际穿越')

    await openUploadAndSelectFile()
    fireEvent.click(screen.getByRole('button', { name: '开始上传' }))

    expect(await screen.findByText('文件大小超出上传上限')).toBeInTheDocument()
    expect(assetsCallCount()).toBe(1)
  })

  it('上传失败（413 且响应为 HTML，nginx 环境）：回落文案特判为 2GB 上限提示，列表不刷新', async () => {
    // 真实 nginx 413 返回 HTML：readErrorDetail 拿不到 detail，回落
    // "请求失败（HTTP 413）"——组件按 413 字样换成可行动文案
    mockUploadApis({ uploadError: new ApiError(413, '请求失败（HTTP 413）') })
    renderAssetList()
    await screen.findByText('星际穿越')

    await openUploadAndSelectFile()
    fireEvent.click(screen.getByRole('button', { name: '开始上传' }))

    expect(await screen.findByText('文件超过 2GB 上传上限')).toBeInTheDocument()
    expect(assetsCallCount()).toBe(1)
    expect(screen.getByText('选择视频文件')).toBeInTheDocument() // Modal 保留可重试
  })
})
