// 路由门禁与菜单测试：App 以 MemoryRouter 驱动（BrowserRouter 由 main.tsx 提供）。
// api/client 整体 mock——门禁只关心 getMe 结果：拒绝 → 登录页；成功 → 布局与菜单
// （八项菜单，其中"闭环评测"/"开放评测"/"配置管理"仅 admin 角色可见；"质量评估"
// 已并入工作台总览、"标签库/导入任务"合一为标签管理、"用户/模型配置"合一为
// 配置管理）。登录态下工作台会挂载并请求三个指标端点，apiFetch 桩默认为永不
// 落定的 Promise，页面停留在加载态。
//
// 路由装配集成用例（2026-11-02 白屏回归增补）：整棵 <App /> 在真实路由嵌套下
// 渲染。各页面单测孤立渲染组件，门禁用例只看菜单项——RequireAuth → Layout →
// AdminRoute → 页面这条链从未被整体测过。react-router v7 的 useOutlet 无条件以
// 传入 context（缺省即 undefined）包 OutletContext.Provider，裸 <Outlet /> 会把
// 祖先提供的 context 重置；Layout 未向下转发登录态时，AdminRoute 的
// useOutletContext 解构 user 直接 TypeError，React 卸载整棵树 → /users 与
// /model-config 白屏（其余页面不经 AdminRoute 不受影响）。
// 注：getMe/login/logout 须显式桩掉——vi.mock 工厂只替换导入方可见的导出，
// client 模块内部 getMe → apiFetch 的同模块引用不经过替换后的 apiFetch。
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { apiFetch, getMe } from './api/client'

vi.mock('./api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api/client')>()
  return {
    ...actual,
    apiFetch: vi.fn(),
    getMe: vi.fn(),
    login: vi.fn(),
    logout: vi.fn(),
  }
})

const mockedGetMe = vi.mocked(getMe)
const mockedApiFetch = vi.mocked(apiFetch)

beforeEach(() => {
  mockedGetMe.mockReset()
  mockedApiFetch.mockReset()
  // 默认不落定：未被用例覆盖的页面停留在加载态、不打真实网络
  mockedApiFetch.mockImplementation(() => new Promise(() => {}))
})

afterEach(() => {
  vi.clearAllMocks()
})

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  )
}

describe('路由门禁（requireAuth）', () => {
  it('未登录：访问工作台被重定向到登录页', async () => {
    mockedGetMe.mockRejectedValue(new Error('未登录'))

    renderAt('/')

    expect(await screen.findByPlaceholderText('用户名')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('密码')).toBeInTheDocument()
    // antd 按钮对两个汉字自动插入空隙（渲染为"登 录"），以宽容正则匹配可访问名
    expect(screen.getByRole('button', { name: /登\s*录/ })).toBeInTheDocument()
  })

  it('admin 登录：八项菜单按序（终态对齐八模块命名）', async () => {
    mockedGetMe.mockResolvedValue({ user_id: 'u1', username: 'admin', role: 'admin' })

    renderAt('/')

    // 等布局渲染完成（任一菜单项出现）
    expect(await screen.findByRole('menuitem', { name: '审核中心' })).toBeInTheDocument()
    const menuItems = screen.getAllByRole('menuitem')
    // 菜单顺序即模块顺序：工作台 → 标签管理 → 内容管理 → 内容打标 →
    // 审核中心 → 闭环评测 → 开放评测 → 配置管理
    expect(menuItems.map((el) => el.textContent)).toEqual([
      '工作台',
      '标签管理',
      '内容管理',
      '内容打标',
      '审核中心',
      '闭环评测',
      '开放评测',
      '配置管理',
    ])
    // 顶部显示用户名
    expect(screen.getByText('admin')).toBeInTheDocument()
  })

  it('普通用户：菜单为终态前五项，不显示 admin 专属三项', async () => {
    mockedGetMe.mockResolvedValue({ user_id: 'u2', username: 'alice', role: 'reviewer' })

    renderAt('/')

    expect(await screen.findByRole('menuitem', { name: '审核中心' })).toBeInTheDocument()
    expect(screen.getAllByRole('menuitem').map((el) => el.textContent)).toEqual([
      '工作台',
      '标签管理',
      '内容管理',
      '内容打标',
      '审核中心',
    ])
    expect(screen.queryByRole('menuitem', { name: '闭环评测' })).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: '开放评测' })).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: '配置管理' })).not.toBeInTheDocument()
  })
})

// ---------- 路由装配集成（白屏回归位） ----------

/** admin 登录态的 apiFetch 桩：按路径分发，覆盖两个管理页所需的全部端点。 */
function mockAdminApis() {
  mockedGetMe.mockResolvedValue({ user_id: 'u-admin', username: 'admin', role: 'admin' })
  mockedApiFetch.mockImplementation(async (path: string) => {
    if (path === '/api/v1/users') {
      return {
        items: [
          { user_id: 'u-admin', username: 'admin', role: 'admin', created_at: null },
        ],
      }
    }
    if (path === '/api/v1/config') {
      return {
        llm_model: 'qwen3.7-plus', llm_temperature: 0.7, recall_top_k_per_type: 50,
        open_match_threshold: 0.9,
        generation_workers: 2, use_mock_llm: false, use_mock_embedder: false,
        database_path: '/data/tagplatform.db',
      }
    }
    if (path === '/api/v1/prompts') {
      // 只读契约视图（Task 14）：无版本字段，仅 mode/正文/占位符白名单与必需项
      return [
        {
          mode: 'closed',
          template_text: '模板 {content} {candidates}',
          allowed_fields: ['title'],
          required_placeholders: ['{content}', '{candidates}'],
        },
      ]
    }
    // 闭环评测页挂载双拉（暂无数据集/历史的空态足够证明装配）
    if (path === '/api/v1/eval/closed/datasets') return []
    if (path === '/api/v1/eval/closed/runs') return []
    // 开放评测页挂载：open 数据集/历史 + closed 活跃探测（跨服务互斥）
    if (path === '/api/v1/eval/open/datasets') return []
    if (path === '/api/v1/eval/open/runs') return []
    if (path === '/api/v1/eval/open/dataset-root') {
      return { root: '/cache/MovieTags', is_default: true }
    }
    // 标签管理导入 tab 挂载拉取导入批次
    if (path === '/api/v1/import/tasks') return []
    throw new Error(`未预期的请求：${path}`)
  })
}

/** reviewer 登录态的 apiFetch 桩：仅工作台指标端点。 */
function mockReviewerApis() {
  mockedGetMe.mockResolvedValue({ user_id: 'u-rev', username: 'reviewer', role: 'reviewer' })
  mockedApiFetch.mockImplementation(async (path: string) => {
    if (path === '/api/v1/metrics/summary') {
      return {
        pending_count: 3,
        review_stats: {
          approved: { count: 0, ratio: null },
          rejected: { count: 0, ratio: null },
          merged: { count: 0, ratio: null },
          modified: { count: 0, ratio: null },
        },
        generated_pass_rate: null,
        evidence_coverage: null,
        task_stats: {
          total: 0, succeeded: 0, failed: 0,
          success_rate: null, avg_duration_ms: null,
        },
      }
    }
    if (path === '/api/v1/metrics/confidence-distribution') return []
    if (path === '/api/v1/metrics/dir-coverage') return []
    throw new Error(`未预期的请求：${path}`)
  })
}

describe('路由装配（Layout → AdminRoute → 页面）', () => {
  it('admin 访问 /config：整树渲染出用户管理页（默认 tab）', async () => {
    // 回归位：AdminRoute 解构 useOutletContext 曾抛 TypeError 白屏——
    // 能等到页面内容即证明 RequireAuth→Layout→AdminRoute→Config→Users 全链路通了
    mockAdminApis()
    renderAt('/config')
    expect(await screen.findByText('创建用户')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: '用户' })).toHaveAttribute('aria-selected', 'true')
  })

  it('admin 访问旧书签 /model-config：重定向到配置管理并激活"模型与 Prompt" tab', async () => {
    mockAdminApis()
    renderAt('/model-config')
    expect(await screen.findByText('Prompt 模板')).toBeInTheDocument()
    expect(screen.getByText('当前运行配置')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: '模型与 Prompt' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
  })

  it('admin 访问 /eval-closed：渲染出闭环评测页（新建表单与历史骨架）', async () => {
    // 装配冒烟：新页签经 RequireAuth→Layout→AdminRoute→ClosedEval 全链路渲染
    mockAdminApis()
    renderAt('/eval-closed')
    expect(await screen.findByText('新建评测')).toBeInTheDocument()
    expect(screen.getByText('评测历史')).toBeInTheDocument()
    // 挂载双拉了数据集与历史端点
    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/eval/closed/datasets')
    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/eval/closed/runs')
  })

  it('admin 访问 /eval-open：渲染出开放评测页（新建表单与历史骨架）', async () => {
    // 装配冒烟：新页签经 RequireAuth→Layout→AdminRoute→OpenEval 全链路渲染
    mockAdminApis()
    renderAt('/eval-open')
    expect(await screen.findByText('新建评测')).toBeInTheDocument()
    expect(screen.getByText('评测历史')).toBeInTheDocument()
    // 挂载拉取 open 数据集/历史端点 + closed 活跃探测（跨服务互斥）
    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/eval/open/datasets')
    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/eval/open/runs')
    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/eval/closed/runs')
  })

  it('admin 访问旧书签 /imports：重定向到标签管理并激活导入批次 tab', async () => {
    // 回归位：旧独立路由删除后书签直达必须落在合并页的正确 tab
    mockAdminApis()
    renderAt('/imports')
    expect(await screen.findByRole('tab', { name: '导入批次' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    // 导入页本体渲染（任务历史卡）
    expect(await screen.findByText('任务历史')).toBeInTheDocument()
    // 旧独立菜单项不再存在
    expect(screen.queryByRole('menuitem', { name: '导入任务' })).not.toBeInTheDocument()
  })

  it('访问旧书签 /quality：重定向回工作台（总览）', async () => {
    // 四条旧路由重定向中唯一无用例的一条（其余 /imports、/users、
    // /model-config 均有集成测试）
    mockReviewerApis()
    renderAt('/quality')
    expect(await screen.findByText('待审核标签')).toBeInTheDocument()
  })

  it('reviewer 直接访问旧书签 /users：重定向链经 AdminRoute 拦回工作台', async () => {
    // 链路：/users → /config?tab=users →（非 admin）AdminRoute → / 工作台
    mockReviewerApis()
    renderAt('/users')
    expect(await screen.findByText('待审核标签')).toBeInTheDocument()
    // 重定向后管理页内容不应出现
    expect(screen.queryByText('创建用户')).not.toBeInTheDocument()
  })
})
