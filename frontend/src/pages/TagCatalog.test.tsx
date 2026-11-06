// 标签库页测试（v2 混合树）：覆盖根视图混合树渲染（kind 徽标 + 目录状态
// "—"）、子级展开（parent_id 混合子级）、q 搜索提交触发、TreeSelect 目录
// 懒加载（kind=dir 只拉目录）、详情抽屉（kind/path/aliases/status）与
// 生命周期按钮仅对 kind=tag（停用/启用切换、删除后跳转上级目录）。
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { Modal } from 'antd'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import TagCatalog from './TagCatalog'
import { apiFetch } from '../api/client'
import type { TagNode, TagNodeDetail } from '../api/catalog'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, apiFetch: vi.fn() }
})

const mockedApiFetch = vi.mocked(apiFetch)

// ---------- 夹具（与后端 TagNodeOut/TagNodeDetail 契约同形） ----------

const ROOT_DIR: TagNode = {
  id: 'dir-root',
  name: '剧情',
  kind: 'dir',
  dir_type: 'dir_static',
  description: '剧情类目录',
  parent_id: null,
  status: null,
  has_children: true,
  aliases: [],
}

const ROOT_TAG_ACTIVE: TagNode = {
  id: 'tag-1',
  name: '反转',
  kind: 'tag',
  dir_type: null,
  description: '情节反转',
  parent_id: null,
  status: 'active',
  has_children: false,
  aliases: ['plot twist'],
}

const ROOT_TAG_DEPRECATED: TagNode = {
  id: 'tag-2',
  name: '烂尾',
  kind: 'tag',
  dir_type: null,
  description: '结局崩坏',
  parent_id: null,
  status: 'deprecated',
  has_children: false,
  aliases: [],
}

/** 无子级的空目录：点击展开无意义，走详情抽屉（无生命周期按钮）。 */
const EMPTY_DIR: TagNode = {
  id: 'dir-empty',
  name: '空目录',
  kind: 'dir',
  dir_type: 'dir_dynamic',
  description: '',
  parent_id: null,
  status: null,
  has_children: false,
  aliases: [],
}

const SUB_DIR: TagNode = {
  id: 'dir-sub',
  name: '悬疑',
  kind: 'dir',
  dir_type: 'dir_static',
  description: '悬疑子目录',
  parent_id: 'dir-root',
  status: null,
  has_children: false,
  aliases: [],
}

/** 有下级的子目录：TreeSelect 下拉树只挂 has_children 的目录（沿 v1 语义）。 */
const SUB_DIR_WITH_CHILDREN: TagNode = {
  ...SUB_DIR,
  id: 'dir-sub-tree',
  has_children: true,
}

const CHILD_TAG: TagNode = {
  id: 'tag-3',
  name: '打斗',
  kind: 'tag',
  dir_type: null,
  description: '动作打斗',
  parent_id: 'dir-root',
  status: 'active',
  has_children: false,
  aliases: ['搏斗'],
}

/** 标签详情（含从根到本节点的路径）。 */
const TAG_DETAIL: TagNodeDetail = {
  id: 'tag-3',
  name: '打斗',
  kind: 'tag',
  dir_type: null,
  description: '动作打斗',
  parent_id: 'dir-root',
  status: 'active',
  has_children: false,
  aliases: ['别名甲', '别名乙'],
  path: ['剧情', '打斗'],
  path_ids: ['dir-root', 'tag-3'],
}

// ---------- mock 装配与查询辅助 ----------

interface MockOptions {
  root?: TagNode[]
  children?: Record<string, TagNode[]>
  search?: TagNode[]
  detail?: TagNodeDetail
  deleteResult?: { tag_id: string; parent_id: string | null; parent_name: string | null }
}

/** 按路径分发的 apiFetch 桩：根视图（root=true）/子级（parent_id，可带
 * kind=dir）/q 搜索/单条详情/DELETE 生命周期各走各的夹具。 */
function mockApis(options: MockOptions = {}) {
  mockedApiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    if (method === 'DELETE' && /^\/api\/v1\/tags\/[^/]+$/.test(path)) {
      return options.deleteResult ?? { tag_id: 'tag-3', parent_id: 'dir-root', parent_name: '剧情' }
    }
    if (path === '/api/v1/tags' || path.startsWith('/api/v1/tags?')) {
      const params = new URLSearchParams(path.split('?')[1] ?? '')
      const parentId = params.get('parent_id')
      let items: TagNode[]
      if (parentId !== null) {
        const all = options.children?.[parentId] ?? []
        // TreeSelect 目录懒加载带 kind=dir：只回子目录
        items = params.get('kind') === 'dir' ? all.filter((n) => n.kind === 'dir') : all
      } else if (params.get('root') === 'true') {
        items = options.root ?? []
      } else {
        items = options.search ?? []
      }
      return { items }
    }
    if (/^\/api\/v1\/tags\/[^/]+\/(activate|deactivate)$/.test(path)) {
      // 生命周期端点回传切换后的节点详情
      return options.detail ?? TAG_DETAIL
    }
    if (/^\/api\/v1\/tags\/[^/]+$/.test(path)) {
      return options.detail ?? TAG_DETAIL
    }
    throw new Error(`未预期的请求：${path}`)
  })
}

/** 目录列表端点的请求路径（按调用顺序，断言筛选参数用）。 */
function catalogCallPaths(): string[] {
  return mockedApiFetch.mock.calls
    .map((call) => call[0])
    .filter((p): p is string => typeof p === 'string' && p.startsWith('/api/v1/tags'))
}

function renderTagCatalog() {
  return render(<TagCatalog />)
}

/** 以 Drawer 容器（.ant-drawer）收窄查询，避开背后 Table 的同名列头。 */
function drawerOf(titleEl: HTMLElement): HTMLElement {
  return titleEl.closest('.ant-drawer') as HTMLElement
}

beforeEach(() => {
  mockedApiFetch.mockReset()
})

afterEach(() => {
  // 命令式 Modal.confirm 挂在 document.body 的 portal 不随 RTL cleanup 卸载；
  // 且 destroyAll 的离场动画在 jsdom 不结束（transitionend 永不触发），DOM
  // 节点会渗入下一用例——destroyAll 触发正规卸载后再直接移除残留容器。
  Modal.destroyAll()
  Array.from(document.body.children).forEach((el) => {
    if (el instanceof HTMLElement && el.querySelector('.ant-modal')) el.remove()
  })
  vi.clearAllMocks()
})

// ---------- 用例 ----------

describe('TagCatalog 标签库页（v2 混合树）', () => {
  it('根视图混合树渲染：kind 徽标（目录/标签）、目录状态为 "—"、deprecated 标签显示停用', async () => {
    mockApis({ root: [ROOT_TAG_ACTIVE, ROOT_DIR, ROOT_TAG_DEPRECATED] })
    renderTagCatalog()

    // 徽标直接取节点 kind：目录行 → 目录徽标 + 状态 "—"（目录无 status 列）
    const dirRow = (await screen.findByText('剧情')).closest('tr')!
    expect(dirRow).toHaveTextContent('目录')
    expect(dirRow).toHaveTextContent('—')
    // 标签行 → 标签徽标 + 状态徽标（deprecated → 停用）
    const activeRow = screen.getByText('反转').closest('tr')!
    expect(activeRow).toHaveTextContent('标签')
    expect(activeRow).toHaveTextContent('启用')
    const deprecatedRow = screen.getByText('烂尾').closest('tr')!
    expect(deprecatedRow).toHaveTextContent('停用')

    // 首屏参数：root=true 显式发送
    expect(catalogCallPaths()).toEqual(['/api/v1/tags?root=true'])
  })

  it('目录与标签混合时目录排在前面', async () => {
    mockApis({ root: [ROOT_TAG_ACTIVE, ROOT_DIR] })
    renderTagCatalog()

    const dirRow = (await screen.findByText('剧情')).closest('tr')!
    const tagRow = screen.getByText('反转').closest('tr')!
    expect(dirRow.compareDocumentPosition(tagRow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('q 搜索：提交（按钮）触发带 q 重新拉取，输入本身不触发', async () => {
    mockApis({ root: [ROOT_TAG_ACTIVE], search: [ROOT_TAG_DEPRECATED] })
    renderTagCatalog()
    await screen.findByText('反转')

    fireEvent.change(screen.getByPlaceholderText('名称/别名搜索'), {
      target: { value: '烂尾' },
    })

    expect(catalogCallPaths()).toHaveLength(1) // 输入即触发则此处已两次

    fireEvent.click(screen.getByRole('button', { name: /搜\s*索/ }))

    await waitFor(() => expect(catalogCallPaths()).toHaveLength(2))
    expect(catalogCallPaths()[1]).toBe(`/api/v1/tags?q=${encodeURIComponent('烂尾')}`)
  })

  it('点击目录行展开并展示混合子级（子目录 + 子标签，parent_id 拉取）', async () => {
    mockApis({
      root: [ROOT_DIR],
      children: { 'dir-root': [SUB_DIR, CHILD_TAG] },
    })
    renderTagCatalog()
    await screen.findByText('剧情')

    fireEvent.click(screen.getByText('剧情'))

    await waitFor(() => {
      expect(
        mockedApiFetch.mock.calls.some(([path]) => path === '/api/v1/tags?parent_id=dir-root'),
      ).toBe(true)
    })
    expect(await screen.findByText('悬疑')).toBeInTheDocument()
    expect(screen.getByText('打斗')).toBeInTheDocument()
    expect(screen.getByText('剧情')).toBeInTheDocument() // 父级行仍在
  })

  it('TreeSelect 目录层级：下拉只展示目录；展开节点懒加载只拉目录（kind=dir）', async () => {
    mockApis({
      root: [ROOT_DIR, CHILD_TAG],
      children: { 'dir-root': [SUB_DIR_WITH_CHILDREN, CHILD_TAG] },
    })
    renderTagCatalog()
    await screen.findByText('剧情')

    fireEvent.mouseDown(screen.getByRole('combobox', { name: '目录层级' }))

    // 下拉树只有目录"剧情"（表格行 + 下拉选项 = 2 处）；叶子标签"打斗"只在表格 = 1 处
    await waitFor(() => expect(screen.getAllByText('剧情')).toHaveLength(2))
    expect(screen.getAllByText('打斗')).toHaveLength(1)

    // 展开下拉树中的"剧情"节点：loadData 传 kind=dir，只回子目录
    const switcher = document.querySelector(
      '.ant-select-tree-switcher:not(.ant-select-tree-switcher-noop)',
    ) as HTMLElement
    fireEvent.click(switcher)

    await waitFor(() => {
      expect(
        catalogCallPaths().some((p) => p === '/api/v1/tags?parent_id=dir-root&kind=dir'),
      ).toBe(true)
    })
    // 下拉树内挂上子目录"悬疑"（表格里没有 → 全页仅 1 处）
    expect(await screen.findByText('悬疑')).toBeInTheDocument()
  })

  it('标签行点击打开详情抽屉：种类/路径面包屑（父目录可跳 scope）/别名/状态', async () => {
    mockApis({ root: [ROOT_DIR, CHILD_TAG] })
    renderTagCatalog()
    await screen.findByText('打斗')

    fireEvent.click(screen.getByText('打斗'))

    await waitFor(() => {
      expect(mockedApiFetch.mock.calls.some(([p]) => p === '/api/v1/tags/tag-3')).toBe(true)
    })
    const drawer = drawerOf(await screen.findByText('节点详情'))
    const kindItem = within(drawer).getByText('种类').closest('.ant-descriptions-item')!
    expect(kindItem).toHaveTextContent('标签')
    expect(within(drawer).getByText('tag-3')).toBeInTheDocument()
    expect(within(drawer).getByText('启用')).toBeInTheDocument()

    // 面包屑：父目录"剧情"可点击跳 scope，本节点为纯文本
    const nameItem = within(drawer).getByText('名称').closest(
      '.ant-descriptions-item',
    ) as HTMLElement
    expect(nameItem).toHaveTextContent('剧情')
    expect(nameItem).toHaveTextContent('打斗')
    fireEvent.click(within(nameItem).getByRole('button', { name: '剧情' }))
    await waitFor(() => {
      expect(
        catalogCallPaths().some((p) => p === '/api/v1/tags?parent_id=dir-root'),
      ).toBe(true)
    })

    // 别名列沿用
    const aliasItem = within(drawer).getByText('别名').closest('.ant-descriptions-item')!
    expect(aliasItem).toHaveTextContent('别名甲')
    expect(aliasItem).toHaveTextContent('别名乙')
  })

  it('目录详情抽屉：种类为目录、状态 "—"，无启用/停用与删除按钮', async () => {
    mockApis({
      root: [EMPTY_DIR],
      detail: { ...EMPTY_DIR, path: ['空目录'], path_ids: ['dir-empty'] },
    })
    renderTagCatalog()
    await screen.findByText('空目录')

    // 空目录无子级可展开 → 行点击走详情抽屉
    fireEvent.click(screen.getByText('空目录'))

    const drawer = drawerOf(await screen.findByText('节点详情'))
    const kindItem = within(drawer).getByText('种类').closest('.ant-descriptions-item')!
    expect(kindItem).toHaveTextContent('目录')
    const statusItem = within(drawer).getByText('状态').closest('.ant-descriptions-item')!
    expect(statusItem).toHaveTextContent('—')
    // 生命周期按钮仅对 kind=tag 渲染：目录节点无启用/停用/删除入口
    // （antd 两字按钮插入空隙，可访问名以宽容正则匹配；v2 后端对目录生命周期操作一律 404）
    expect(within(drawer).queryByRole('button', { name: /停\s*用/ })).not.toBeInTheDocument()
    expect(within(drawer).queryByRole('button', { name: /启\s*用/ })).not.toBeInTheDocument()
    expect(within(drawer).queryByRole('button', { name: /删\s*除/ })).not.toBeInTheDocument()
  })

  it('生命周期仅标签：deprecated 详情显示"启用"并可切换；删除确认后跳转上级目录', async () => {
    const detail: TagNodeDetail = { ...ROOT_TAG_DEPRECATED, path: ['剧情', '烂尾'], path_ids: ['dir-root', 'tag-2'] }
    mockApis({
      root: [ROOT_DIR, ROOT_TAG_DEPRECATED],
      children: { 'dir-root': [] },
      detail,
    })
    renderTagCatalog()
    await screen.findByText('烂尾')

    fireEvent.click(screen.getByText('烂尾'))
    const drawer = drawerOf(await screen.findByText('节点详情'))

    // deprecated → 显示"启用"按钮；点击后状态徽标翻转为"停用"（回传 active 详情）
    mockedApiFetch.mockImplementation(async (path: string) => {
      if (/\/activate$/.test(path)) return { ...detail, status: 'active' }
      throw new Error(`未预期的请求：${path}`)
    })
    fireEvent.click(within(drawer).getByRole('button', { name: /启\s*用/ }))
    expect(await within(drawer).findByRole('button', { name: /停\s*用/ })).toBeInTheDocument()

    // 删除：确认弹窗 → 确认 → DELETE → 详情关闭并跳转上级目录（返回上级目录行）
    mockedApiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      if (method === 'DELETE' && path === '/api/v1/tags/tag-2') {
        return { tag_id: 'tag-2', parent_id: 'dir-root', parent_name: '剧情' }
      }
      if (path === '/api/v1/tags?parent_id=dir-root') return { items: [] }
      throw new Error(`未预期的请求：${method} ${path}`)
    })
    fireEvent.click(within(drawer).getByRole('button', { name: /删\s*除/ }))
    await screen.findAllByText('删除标签') // 确认弹窗标题（antd confirm 标题渲染两处）
    const modal = screen.getAllByText('删除标签')[0].closest('.ant-modal') as HTMLElement
    fireEvent.click(within(modal).getByRole('button', { name: /删\s*除/ }))

    await waitFor(() => {
      expect(
        mockedApiFetch.mock.calls.some(
          ([p, init]) => p === '/api/v1/tags/tag-2' && init?.method === 'DELETE',
        ),
      ).toBe(true)
    })
    // 删除后跳转 result.parent_id 对应 scope：出现"返回上级目录"行
    expect(await screen.findByText(/返回上级目录/)).toBeInTheDocument()
  })

  it('目录加载失败：渲染错误提示（透出错误文案）', async () => {
    mockedApiFetch.mockImplementation(async () => {
      throw new Error('后端不可用')
    })
    renderTagCatalog()

    expect(await screen.findByText('标签库加载失败')).toBeInTheDocument()
    expect(screen.getByText('后端不可用')).toBeInTheDocument()
  })
})
