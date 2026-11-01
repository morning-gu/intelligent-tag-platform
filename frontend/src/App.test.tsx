// 路由门禁与菜单测试：App 以 MemoryRouter 驱动（BrowserRouter 由 main.tsx 提供）。
// api/client 整体 mock——门禁只关心 getMe 结果：拒绝 → 登录页；成功 → 布局与菜单
// （九项菜单齐全，其中"用户管理"/"模型配置"仅 admin 角色可见）。登录态下工作台
// 会挂载并请求指标接口，apiFetch 桩为永不落定的 Promise，页面停留在加载态、不打网络。
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { getMe } from './api/client'

vi.mock('./api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api/client')>()
  return {
    ...actual,
    getMe: vi.fn(),
    apiFetch: vi.fn(() => new Promise(() => {})),
  }
})

const mockedGetMe = vi.mocked(getMe)

beforeEach(() => {
  mockedGetMe.mockReset()
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

  it('admin 登录：九项菜单齐全（含仅 admin 可见的用户管理与模型配置）', async () => {
    mockedGetMe.mockResolvedValue({ user_id: 'u1', username: 'admin', role: 'admin' })

    renderAt('/')

    // 等布局渲染完成（任一菜单项出现）
    expect(await screen.findByRole('menuitem', { name: '审核中心' })).toBeInTheDocument()
    const menuItems = screen.getAllByRole('menuitem')
    expect(menuItems).toHaveLength(9)
    expect(screen.getByRole('menuitem', { name: '工作台' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: '内容资产' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: '生产任务' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: '标签导入' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: '标签库' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: '质量评估' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: '用户管理' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: '模型配置' })).toBeInTheDocument()
    // 顶部显示用户名
    expect(screen.getByText('admin')).toBeInTheDocument()
  })

  it('普通用户：菜单仅七项，不显示用户管理与模型配置', async () => {
    mockedGetMe.mockResolvedValue({ user_id: 'u2', username: 'alice', role: 'reviewer' })

    renderAt('/')

    expect(await screen.findByRole('menuitem', { name: '审核中心' })).toBeInTheDocument()
    expect(screen.getAllByRole('menuitem')).toHaveLength(7)
    expect(screen.queryByRole('menuitem', { name: '用户管理' })).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: '模型配置' })).not.toBeInTheDocument()
  })
})
