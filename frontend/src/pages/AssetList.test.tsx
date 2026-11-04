// 内容资产列表测试：mock apiFetch 按路径分发，覆盖：
// - 按类型分组、默认折叠、展开后渲染行（时长/视频/创建时间与 null 兜底）；
// - 场景描述在列表里截断预览，点行进入详情路由；
// - 标题搜索传参（提交触发而非输入即触发）；
// - 上传：未命中 → 占位登记提示；命中 → Modal 内轮询生成进度并刷新列表；
// - 422/413 失败透出后端 detail 且不刷新列表（nginx HTML 413 特判）。
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import AssetList from './AssetList'
import { ApiError, apiFetch } from '../api/client'
import type { AssetSummary } from '../api/assets'
import type { BuildTask, MediaUploadResult } from '../api/media'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, apiFetch: vi.fn() }
})

const mockedApiFetch = vi.mocked(apiFetch)

// ---------- 夹具（与后端 AssetOut 契约同形） ----------

const LONG_SCENE_TITLE =
  'The Dude 抵达 The Big Lebowski 的豪宅，Brandt 先带他参观了展示 Lebowski 慈善成就的奖项。' +
  '随后在书房，坐轮椅的 The Big Lebowski 态度强硬，拒绝为被毁的地毯负责。'

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
    title: LONG_SCENE_TITLE,
    duration_ms: 65_000,
    video_uri: null,
    created_at: null,
  },
  {
    asset_id: 'a-series',
    asset_type: 'series',
    parent_id: null,
    title: '继承之战',
    duration_ms: null,
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

/** 展开某个类型分组（分组头文字 = 类型中文名）。 */
async function expandGroup(label: string) {
  fireEvent.click(await screen.findByText(label))
}

// ---------- 上传场景夹具（与后端 MediaUploadResponse / BuildTaskOut 同形） ----------

const UNMATCHED_UPLOAD: MediaUploadResult = {
  asset_id: 'a-new',
  title: '新片',
  video_uri: 'C:\\data\\media\\u.mkv',
  movie_id: null,
  matched_title: null,
  match_how: null,
  build_task_id: null,
}

const MATCHED_UPLOAD: MediaUploadResult = {
  ...UNMATCHED_UPLOAD,
  movie_id: 'tt0172495',
  matched_title: '角斗士',
  match_how: 'imdb_id',
  build_task_id: 'build_1',
}

const RUNNING_TASK: BuildTask = {
  task_id: 'build_1',
  asset_id: 'a-new',
  movie_id: 'tt0172495',
  status: 'running',
  total_scenes: 4,
  done_scenes: 2,
  error: null,
}

const DONE_TASK: BuildTask = { ...RUNNING_TASK, status: 'succeeded', done_scenes: 4 }

const NEW_ASSET: AssetSummary = {
  asset_id: 'a-new',
  asset_type: 'movie',
  parent_id: null,
  title: '新片',
  duration_ms: null,
  video_uri: 'C:\\data\\media\\u.mkv',
  created_at: '2026-11-01T10:00:00',
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

/** 上传场景的 apiFetch 桩：资产列表 + /media/upload + 进度查询。 */
function mockUploadApis(
  options: {
    uploadError?: Error
    uploadResult?: MediaUploadResult
    tasks?: BuildTask[]
  } = {},
) {
  let assets: AssetSummary[] = ASSETS
  const tasks = [...(options.tasks ?? [RUNNING_TASK, DONE_TASK])]
  mockedApiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    if (method === 'POST' && path === '/api/v1/media/upload') {
      if (options.uploadError) throw options.uploadError
      assets = [NEW_ASSET, ...ASSETS] // 上传成功：后端新增资产行
      return options.uploadResult ?? UNMATCHED_UPLOAD
    }
    if (path.startsWith('/api/v1/media/build-tasks/')) {
      const next = tasks.length > 1 ? tasks.shift()! : tasks[0]
      return next
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
  it('按类型分组、默认折叠：只显示分组头与条数', async () => {
    renderAssetList()

    expect(await screen.findByText('电影')).toBeInTheDocument()
    expect(screen.getByText('剧集')).toBeInTheDocument()
    expect(screen.getByText('场景')).toBeInTheDocument()
    // 默认折叠：分组内的资产行不渲染
    expect(screen.queryByText('星际穿越')).not.toBeInTheDocument()
    // 上传按钮旁的文件名要求提示直接可见
    expect(
      screen.getByText('文件名需含 IMDb ID（如 tt0068646）或英文片名（如 The.Godfather.1972）'),
    ).toBeInTheDocument()
    expect(assetsCallPaths()).toEqual(['/api/v1/assets'])
  })

  it('展开电影分组：渲染时长/视频/创建时间与 null 兜底', async () => {
    renderAssetList()
    await expandGroup('电影')

    const row = (await screen.findByText('星际穿越')).closest('tr')!
    // 10_800_000ms → 180:00（formatTimestamp 复用，分钟位不封顶）
    expect(row).toHaveTextContent('180:00')
    expect(row).toHaveTextContent('可播放')
    expect(row).toHaveTextContent('2026-10-30 08:30:00')

    await expandGroup('剧集')
    const rowB = (await screen.findByText('继承之战')).closest('tr')!
    expect(rowB).toHaveTextContent('—')
    expect(rowB).toHaveTextContent('无')
  })

  it('场景描述在列表里截断预览（不显示全文）', async () => {
    renderAssetList()
    await expandGroup('场景')

    const cell = await screen.findByText((content) => content.startsWith('The Dude 抵达'))
    expect(cell.textContent).toHaveLength(61) // 60 字 + 省略号
    expect(cell.textContent?.endsWith('…')).toBe(true)
    expect(cell.textContent).not.toBe(LONG_SCENE_TITLE)
  })

  it('行点击进入详情路由 /assets/{asset_id}', async () => {
    renderAssetList()
    await expandGroup('场景')

    fireEvent.click(await screen.findByText((content) => content.startsWith('The Dude 抵达')))

    expect(await screen.findByText('详情页占位')).toBeInTheDocument()
  })

  it('标题搜索：提交（按钮）触发带 q 重新拉取，输入本身不触发', async () => {
    renderAssetList()
    await screen.findByText('电影')

    fireEvent.change(screen.getByPlaceholderText('标题搜索'), {
      target: { value: '星际' },
    })
    expect(assetsCallPaths()).toHaveLength(1) // 输入即触发则此处已两次

    fireEvent.click(screen.getByRole('button', { name: /搜\s*索/ }))

    await waitFor(() => expect(assetsCallPaths()).toHaveLength(2))
    expect(assetsCallPaths()[1]).toBe(`/api/v1/assets?q=${encodeURIComponent('星际')}`)
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

describe('上传视频与生成进度', () => {
  it('未匹配影片：占位登记提示（提示文件名规则）+ 列表刷新 + Modal 关闭', async () => {
    mockUploadApis()
    renderAssetList()
    await screen.findByText('电影')

    const file = await openUploadAndSelectFile()
    expect(screen.getByText('已选择：新片.mkv')).toBeInTheDocument()
    // 上传按钮旁只给文件名要求（IMDb ID / 英文片名），不放其他说明
    expect(
      screen.getByText('文件名需含 IMDb ID（如 tt0068646）或英文片名（如 The.Godfather.1972）'),
    ).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '开始上传' }))

    await waitFor(() => expect(uploadCall()).toBeDefined())
    const body = uploadCall()![1].body as FormData
    expect(body).toBeInstanceOf(FormData)
    expect(body.get('file')).toBe(file)

    expect(
      await screen.findByText('已登记「新片」，未匹配到影片——文件名需含 IMDb ID 或英文片名'),
    ).toBeInTheDocument()

    await finishModalLeaveMotion()
    await waitFor(() => expect(screen.queryByText('选择视频文件')).not.toBeInTheDocument())
    await waitFor(() => expect(assetsCallCount()).toBe(2))
    // 新资产出现在电影分组里（分组默认折叠：展开后才可见）
    await expandGroup('电影')
    expect(await screen.findByText('新片')).toBeInTheDocument()
  })

  it('命中影片：Modal 切换为生成进度视图并轮询到完成', async () => {
    mockUploadApis({ uploadResult: MATCHED_UPLOAD })
    renderAssetList()
    await screen.findByText('电影')

    await openUploadAndSelectFile('角斗士.mp4')
    fireEvent.click(screen.getByRole('button', { name: '开始上传' }))

    // 进度视图：匹配片名 + 进度条 + 场景计数
    expect(await screen.findByText('已匹配影片《角斗士》')).toBeInTheDocument()
    expect(await screen.findByTestId('build-progress')).toBeInTheDocument()
    expect(await screen.findByText('生成中 · 2/4 场景')).toBeInTheDocument()

    // 下一轮轮询（2s 后）返回 succeeded → 提示完成并刷新列表
    expect(await screen.findByText('已完成 · 4/4 场景', undefined, { timeout: 5000 })).toBeInTheDocument()
    await waitFor(() => expect(assetsCallCount()).toBe(2))
  })

  it('未选文件时确认按钮禁用，取消按钮关闭 Modal', async () => {
    mockUploadApis()
    renderAssetList()
    await screen.findByText('电影')

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
    await screen.findByText('电影')

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
    await screen.findByText('电影')

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
    await screen.findByText('电影')

    await openUploadAndSelectFile()
    fireEvent.click(screen.getByRole('button', { name: '开始上传' }))

    expect(await screen.findByText('文件超过 2GB 上传上限')).toBeInTheDocument()
    expect(assetsCallCount()).toBe(1)
    expect(screen.getByText('选择视频文件')).toBeInTheDocument() // Modal 保留可重试
  })
})
