// 内容资产列表测试：mock apiFetch 按路径分发，覆盖：
// - 按类型分组、默认折叠、展开后渲染行（时长/视频/内容/更新时间与 null 兜底）；
// - 场景描述在列表里截断预览，点行进入详情路由；
// - 标题搜索传参（提交触发而非输入即触发）；
// - 上传：未命中 → 占位登记提示；命中 → Modal 内轮询生成进度并刷新列表；
// - 422/413 失败透出后端 detail 且不刷新列表（nginx HTML 413 特判）。
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import AssetList from './AssetList'
import { apiFetch } from '../api/client'
import type { AssetSummary } from '../api/assets'
import type { BuildTask, MediaMatchResult, MediaUploadResult } from '../api/media'

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
    has_content: true,
    created_at: '2026-10-30T08:30:00',
    updated_at: '2026-11-02T09:10:00',
  },
  {
    asset_id: 'a-scene',
    asset_type: 'scene',
    parent_id: 'a-movie',
    title: LONG_SCENE_TITLE,
    duration_ms: 65_000,
    video_uri: null,
    has_content: false,
    created_at: null,
    updated_at: null,
  },
  {
    asset_id: 'a-series',
    asset_type: 'series',
    parent_id: null,
    title: '继承之战',
    duration_ms: null,
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

/** 选文件预检的默认应答：没命中（不干扰原有上传用例）。 */
const NO_MATCH: MediaMatchResult = {
  matched: false,
  movie_id: null,
  matched_title: null,
  match_how: null,
  asset_id: null,
  has_content: false,
  content_source: null,
  scene_count: 0,
  built_at: null,
  can_rebuild: true,
}

/** 已生成过的影片：选完文件就该被拦在上传之前。 */
const MATCH_WITH_CONTENT: MediaMatchResult = {
  matched: true,
  movie_id: 'tt0172495',
  matched_title: '角斗士',
  match_how: 'imdb_id',
  asset_id: 'movie_tt0172495',
  has_content: true,
  content_source: 'asset_builder:v5-infer',
  scene_count: 7,
  built_at: '2026-11-04T02:30:00',
  can_rebuild: true,
}

/** 上传成功后列表该多一行（finishUpload 触发）。 */
let markUploaded: () => void = () => {}

/** 资产列表 + 预检 + 进度查询的 apiFetch 桩；上传改走 XHR（见 MockXhr）。 */
function mockUploadApis(
  options: {
    match?: MediaMatchResult
    tasks?: BuildTask[]
  } = {},
) {
  const tasks = [...(options.tasks ?? [RUNNING_TASK, DONE_TASK])]
  let uploaded = false
  markUploaded = () => {
    uploaded = true
  }
  mockedApiFetch.mockImplementation(async (path: string) => {
    if (path.startsWith('/api/v1/media/match')) return options.match ?? NO_MATCH
    if (path.startsWith('/api/v1/media/build-tasks/')) {
      const next = tasks.length > 1 ? tasks.shift()! : tasks[0]
      return next
    }
    if (path === '/api/v1/assets' || path.startsWith('/api/v1/assets?')) {
      return uploaded ? [NEW_ASSET, ...ASSETS] : ASSETS
    }
    throw new Error(`未预期的请求：${path}`)
  })
}

// ---------- XMLHttpRequest 桩：上传走 XHR 才能拿到 upload.onprogress ----------

class MockXhr {
  upload: { onprogress: ((event: ProgressEvent) => void) | null } = { onprogress: null }
  status = 0
  responseText = ''
  withCredentials = false
  onload: (() => void) | null = null
  onerror: (() => void) | null = null

  open(): void {}

  send(body: FormData): void {
    lastUploadForm = body
    activeXhr = this
  }
}

let activeXhr: MockXhr | null = null
let lastUploadForm: FormData | null = null

/** 模拟传输进度（percent 0-100）；act 包裹避免 React 状态更新告警。 */
function emitUploadProgress(percent: number): void {
  act(() => {
    activeXhr?.upload.onprogress?.({
      lengthComputable: true,
      loaded: percent,
      total: 100,
    } as ProgressEvent)
  })
}

/** 模拟上传结束；非 2xx 即失败路径（响应体按后端 detail 形状给）。 */
async function finishUpload(status = 200, body?: string): Promise<void> {
  const xhr = activeXhr
  if (xhr === null) throw new Error('上传尚未开始')
  await act(async () => {
    xhr.status = status
    xhr.responseText = body ?? JSON.stringify(UNMATCHED_UPLOAD)
    if (status >= 200 && status < 300) markUploaded()
    xhr.onload?.()
  })
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
  // 选完文件会异步打 /media/match：等预检落地，否则确认按钮还在
  // checkingFile 的禁用态，后续点击不会触发上传
  await waitFor(() => expect(screen.queryByText('正在识别文件名…')).not.toBeInTheDocument())
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
    if (path.startsWith('/api/v1/media/match')) return NO_MATCH
    throw new Error(`未预期的请求：${path}`)
  })
  activeXhr = null
  lastUploadForm = null
  markUploaded = () => {}
  vi.stubGlobal('XMLHttpRequest', MockXhr)
})

afterEach(() => {
  vi.unstubAllGlobals()
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

  it('展开电影分组：渲染时长/视频/内容/更新时间与 null 兜底', async () => {
    renderAssetList()
    await expandGroup('电影')

    const row = (await screen.findByText('星际穿越')).closest('tr')!
    // 10_800_000ms → 180:00（formatTimestamp 复用，分钟位不封顶）
    expect(row).toHaveTextContent('180:00')
    expect(row).toHaveTextContent('可播放')
    expect(row).toHaveTextContent('已生成')
    expect(row).toHaveTextContent('2026-11-02 09:10:00')

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
    expect(screen.getByText(/已选择：新片\.mkv/)).toBeInTheDocument()
    // 上传按钮旁只给文件名要求（IMDb ID / 英文片名），不放其他说明
    expect(
      screen.getByText('文件名需含 IMDb ID（如 tt0068646）或英文片名（如 The.Godfather.1972）'),
    ).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '开始上传' }))

    await waitFor(() => expect(activeXhr).not.toBeNull())
    expect(lastUploadForm).toBeInstanceOf(FormData)
    expect(lastUploadForm?.get('file')).toBe(file)
    expect(lastUploadForm?.get('force')).toBeNull() // 常规上传不带 force

    await finishUpload()

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
    mockUploadApis()
    renderAssetList()
    await screen.findByText('电影')

    await openUploadAndSelectFile('角斗士.mp4')
    fireEvent.click(screen.getByRole('button', { name: '开始上传' }))

    await waitFor(() => expect(activeXhr).not.toBeNull())
    await finishUpload(200, JSON.stringify(MATCHED_UPLOAD))

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
    expect(activeXhr).toBeNull()
  })

  it('上传进度：显示百分比，传到 100% 后提示服务端仍在处理', async () => {
    mockUploadApis()
    renderAssetList()
    await screen.findByText('电影')

    await openUploadAndSelectFile('角斗士.mp4')
    fireEvent.click(screen.getByRole('button', { name: '开始上传' }))
    await waitFor(() => expect(activeXhr).not.toBeNull())

    emitUploadProgress(37)
    expect(await screen.findByText('正在上传 37%')).toBeInTheDocument()

    emitUploadProgress(100)
    expect(await screen.findByText('上传完成，服务器正在存盘登记…')).toBeInTheDocument()

    await finishUpload(200, JSON.stringify(MATCHED_UPLOAD))
    expect(await screen.findByText('已匹配影片《角斗士》')).toBeInTheDocument()
  })

  it('已生成过的影片：选完文件先问"是否重新生成"，确认后才放行', async () => {
    mockUploadApis({ match: MATCH_WITH_CONTENT })
    renderAssetList()
    await screen.findByText('电影')

    await openUploadAndSelectFile('角斗士.mp4')

    // 预检命中且已有内容 → 提示是否重新生成 + 确认按钮禁用
    expect(
      await screen.findByText('该视频已有对应资产，是否重新生成？'),
    ).toBeInTheDocument()
    expect(screen.getByText(/7 个场景/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '开始上传' })).toBeDisabled()

    // 选"重新生成" → 提示将覆盖 + 按钮可点
    fireEvent.click(screen.getByRole('button', { name: '重新生成（覆盖现有内容）' }))
    expect(
      await screen.findByText('将清空该片现有的资产内容，重新跑一遍生成'),
    ).toBeInTheDocument()
    const ok = screen.getByRole('button', { name: '开始上传' })
    await waitFor(() => expect(ok).not.toBeDisabled())

    fireEvent.click(ok)
    await waitFor(() => expect(activeXhr).not.toBeNull())
    // 重新生成不再靠 force 开关：建了任务就一定真跑
    expect(lastUploadForm?.get('force')).toBeNull()
  })

  it('文件名认不出影片、但库里已有同名资产：照样提示，且不给"重新生成"', async () => {
    mockUploadApis({
      match: {
        ...MATCH_WITH_CONTENT,
        movie_id: null,
        match_how: 'asset_title',
        can_rebuild: false,
      },
    })
    renderAssetList()
    await screen.findByText('电影')

    await openUploadAndSelectFile('角斗士.1080p.mp4')

    expect(
      await screen.findByText('该视频已有对应资产（重复上传会被拒绝）'),
    ).toBeInTheDocument()
    // 认不出是哪部片 → 只能去已有资产看，不能在这里重建
    expect(
      screen.queryByRole('button', { name: '重新生成（覆盖现有内容）' }),
    ).toBeNull()
    expect(screen.getByRole('button', { name: '打开已有资产' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '开始上传' })).toBeDisabled()
  })

  it('预检本身失败：显式提示，不静默放行', async () => {
    mockedApiFetch.mockImplementation(async (path: string) => {
      if (path.startsWith('/api/v1/media/match')) {
        throw new Error('boom')
      }
      if (path.startsWith('/api/v1/assets')) return ASSETS
      return []
    })
    renderAssetList()
    await screen.findByText('电影')

    await openUploadAndSelectFile('新片.mkv')

    expect(
      await screen.findByText('重复检查失败：识别文件名时出错了，仍可继续上传'),
    ).toBeInTheDocument()
  })

  it('上传失败（422 非白名单后缀）：透出后端 detail，Modal 保留可重试，列表不刷新', async () => {
    mockUploadApis()
    renderAssetList()
    await screen.findByText('电影')

    await openUploadAndSelectFile()
    fireEvent.click(screen.getByRole('button', { name: '开始上传' }))
    await waitFor(() => expect(activeXhr).not.toBeNull())
    await finishUpload(422, JSON.stringify({ detail: '不支持的视频格式' }))

    expect(await screen.findByText('不支持的视频格式')).toBeInTheDocument()
    expect(assetsCallCount()).toBe(1) // 未刷新
    expect(screen.getByText('选择视频文件')).toBeInTheDocument() // Modal 仍在
    expect(screen.getByText(/已选择：新片\.mkv/)).toBeInTheDocument() // 已选文件保留供重试
  })

  it('上传失败（413 超 2GB 上限）：透出后端 detail，列表不刷新', async () => {
    mockUploadApis()
    renderAssetList()
    await screen.findByText('电影')

    await openUploadAndSelectFile()
    fireEvent.click(screen.getByRole('button', { name: '开始上传' }))
    await waitFor(() => expect(activeXhr).not.toBeNull())
    await finishUpload(413, JSON.stringify({ detail: '文件大小超出上传上限' }))

    expect(await screen.findByText('文件大小超出上传上限')).toBeInTheDocument()
    expect(assetsCallCount()).toBe(1)
  })

  it('上传失败（413 且响应为 HTML，nginx 环境）：回落文案特判为 2GB 上限提示，列表不刷新', async () => {
    // 真实 nginx 413 返回 HTML：解析不出 detail，回落 "请求失败（HTTP 413）"
    // ——组件按 413 字样换成可行动文案
    mockUploadApis()
    renderAssetList()
    await screen.findByText('电影')

    await openUploadAndSelectFile()
    fireEvent.click(screen.getByRole('button', { name: '开始上传' }))
    await waitFor(() => expect(activeXhr).not.toBeNull())
    await finishUpload(413, '<html><body>413 Request Entity Too Large</body></html>')

    expect(await screen.findByText('文件超过 2GB 上传上限')).toBeInTheDocument()
    expect(assetsCallCount()).toBe(1)
    expect(screen.getByText('选择视频文件')).toBeInTheDocument() // Modal 保留可重试
  })

  it('默认用大模型补字段：上传带 infer=true', async () => {
    mockUploadApis()
    renderAssetList()
    await screen.findByText('电影')

    await openUploadAndSelectFile('角斗士.mp4')
    const box = screen.getByRole('checkbox', { name: '使用大模型补充资产字段' })
    expect(box).toBeChecked()
    // 预检只问"有没有资产"，与口径无关
    const calls = mockedApiFetch.mock.calls.map((call) => String(call[0]))
    expect(calls.some((p) => p.includes('/media/match'))).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: '开始上传' }))
    await waitFor(() => expect(activeXhr).not.toBeNull())
    expect(lastUploadForm?.get('infer')).toBe('true')
  })

  it('取消勾选：只切口径，不重跑预检；上传带 infer=false', async () => {
    mockUploadApis()
    renderAssetList()
    await screen.findByText('电影')

    await openUploadAndSelectFile('角斗士.mp4')
    const before = mockedApiFetch.mock.calls.length
    const box = screen.getByRole('checkbox', { name: '使用大模型补充资产字段' })
    fireEvent.click(box)

    expect(
      await screen.findByText('只填数据集已有的事实（场景描述、字幕、时间），不调用模型'),
    ).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: '使用大模型补充资产字段' })).not.toBeChecked()
    // 口径不影响"有没有资产"，不该再查一次预检
    expect(mockedApiFetch.mock.calls.length).toBe(before)

    fireEvent.click(screen.getByRole('button', { name: '开始上传' }))
    await waitFor(() => expect(activeXhr).not.toBeNull())
    expect(lastUploadForm?.get('infer')).toBe('false')
  })
})
describe('从目录导入资产', () => {
  // 目录浏览的桩：父目录里只有 movie_asset 一个文件夹，点进去才有影片
  const ROOT_PATH = '/cache/h50065879/movie_asset'

  const DIR_ENTRIES = [
    {
      name: 'movie_asset',
      path: `${ROOT_PATH}/movie_asset`,
      is_movie: false,
      movies: 2,
      scenes: 0,
      frames: 0,
      asset_id: null,
      exists: false,
      error: null,
    },
    {
      name: '_raw',
      path: `${ROOT_PATH}/_raw`,
      is_movie: false,
      movies: 0,
      scenes: 0,
      frames: 0,
      asset_id: null,
      exists: false,
      error: null,
    },
  ]

  const MOVIE_ENTRIES = [
    {
      name: 'Rear Window--tt0047396',
      path: `${ROOT_PATH}/movie_asset/Rear Window--tt0047396`,
      is_movie: true,
      movies: 0,
      scenes: 9,
      frames: 27,
      asset_id: 'movie_tt0047396',
      exists: false,
      error: null,
    },
    {
      name: 'Taxi Driver--tt0075314',
      path: `${ROOT_PATH}/movie_asset/Taxi Driver--tt0075314`,
      is_movie: true,
      movies: 0,
      scenes: 8,
      frames: 24,
      asset_id: 'movie_tt0075314',
      exists: true,
      error: null,
    },
    {
      name: 'Broken--tt0000000',
      path: `${ROOT_PATH}/movie_asset/Broken--tt0000000`,
      is_movie: true,
      movies: 0,
      scenes: 0,
      frames: 0,
      asset_id: null,
      exists: false,
      error: '缺少 content.json',
    },
  ]

  /** 浏览 + 导入的 apiFetch 桩；导入后列表多一部片。 */
  function mockDirApis(
    options: { browseError?: Error; bulk?: Record<string, unknown> } = {},
  ) {
    let imported = false
    mockedApiFetch.mockImplementation(async (path: string) => {
      if (path.startsWith('/api/v1/content/import/browse')) {
        if (options.browseError) throw options.browseError
        if (path.includes('movie_asset')) {
          return { path: `${ROOT_PATH}/movie_asset`, parent: ROOT_PATH, entries: MOVIE_ENTRIES }
        }
        return { path: ROOT_PATH, parent: '/cache/h50065879', entries: DIR_ENTRIES }
      }
      if (path === '/api/v1/content/import/movie-bulk') {
        imported = true
        return (
          options.bulk ?? {
            imported: 1,
            skipped: 0,
            failed: 0,
            items: [
              {
                dir_name: 'Rear Window--tt0047396',
                status: 'imported',
                asset_id: 'movie_tt0047396',
                title: 'Rear Window',
                scenes: 9,
                frames: 27,
                reason: null,
              },
            ],
          }
        )
      }
      if (path === '/api/v1/assets' || path.startsWith('/api/v1/assets?')) {
        return imported
          ? [
              ...ASSETS,
              {
                asset_id: 'movie_tt0047396',
                asset_type: 'movie',
                parent_id: null,
                title: 'Rear Window',
                duration_ms: 6_000_000,
                video_uri: null,
                has_content: true,
                created_at: '2026-11-11T10:00:00',
                updated_at: '2026-11-11T10:00:00',
              },
            ]
          : ASSETS
      }
      throw new Error(`未预期的请求：${path}`)
    })
  }

  it('先浏览目录：显示文件夹与影片目录类型，逐层点进目标目录', async () => {
    mockDirApis()
    renderAssetList()
    await screen.findByText('电影')

    fireEvent.click(screen.getByRole('button', { name: '从目录导入' }))

    // 落点是后端默认根：只看到文件夹，没有可导入的片
    expect(await screen.findByText(ROOT_PATH)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /movie_asset/ })).toBeInTheDocument()
    expect(screen.getByText(/含 2 部/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '导入所选（0）' })).toBeInTheDocument()

    // 点文件夹名进入下一层：出现影片目录 + 默认勾选可导入的
    fireEvent.click(screen.getByRole('button', { name: /movie_asset/ }))

    expect(await screen.findByText('Rear Window--tt0047396')).toBeInTheDocument()
    expect(screen.getAllByText('影片目录').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: '导入所选（1）' })).toBeInTheDocument()
    expect(screen.getByText('可导入')).toBeInTheDocument()
    expect(screen.getByText('已导入')).toBeInTheDocument()
    expect(screen.getByText('无法导入')).toBeInTheDocument()
  })

  it('路径栏可直接粘贴绝对路径打开', async () => {
    mockDirApis()
    renderAssetList()
    await screen.findByText('电影')

    fireEvent.click(screen.getByRole('button', { name: '从目录导入' }))
    await screen.findByText(ROOT_PATH)

    fireEvent.change(screen.getByLabelText('目录路径'), {
      target: { value: `${ROOT_PATH}/movie_asset` },
    })
    fireEvent.click(screen.getByRole('button', { name: /打\s*开/ }))

    expect(await screen.findByText('Rear Window--tt0047396')).toBeInTheDocument()
  })

  it('确认导入：POST 当前目录 + 勾选的影片名，成功提示 + 刷新列表', async () => {
    mockDirApis()
    renderAssetList()
    await screen.findByText('电影')

    fireEvent.click(screen.getByRole('button', { name: '从目录导入' }))
    await screen.findByText(ROOT_PATH)
    fireEvent.click(screen.getByRole('button', { name: /movie_asset/ }))
    await screen.findByText('Rear Window--tt0047396')

    fireEvent.click(screen.getByRole('button', { name: '导入所选（1）' }))

    await waitFor(() =>
      expect(
        mockedApiFetch.mock.calls.some(
          (call) => call[0] === '/api/v1/content/import/movie-bulk',
        ),
      ).toBe(true),
    )
    expect(await screen.findByText(/导入完成：成功 1 · 跳过 0 · 失败 0/)).toBeInTheDocument()

    const bulkCall = mockedApiFetch.mock.calls.find(
      (call) => call[0] === '/api/v1/content/import/movie-bulk',
    )
    const body = JSON.parse(String((bulkCall?.[1] as RequestInit).body))
    expect(body.dir_names).toEqual(['Rear Window--tt0047396'])
    expect(body.root).toBe(`${ROOT_PATH}/movie_asset`)
    expect(body.overwrite).toBe(false)

    await waitFor(() => expect(assetsCallPaths().length).toBeGreaterThanOrEqual(2))
  })

  it('目录读取失败：Modal 内显式报错，不静默', async () => {
    mockDirApis({ browseError: new Error('影片资产目录不存在: /nope') })
    renderAssetList()
    await screen.findByText('电影')

    fireEvent.click(screen.getByRole('button', { name: '从目录导入' }))

    expect(await screen.findByText('目录读取失败')).toBeInTheDocument()
    expect(screen.getByText('影片资产目录不存在: /nope')).toBeInTheDocument()
  })
})