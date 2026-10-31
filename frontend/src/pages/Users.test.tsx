// 用户管理页测试（Task 13）：mock apiFetch 按路径/方法分发，覆盖：账号表
// 渲染（角色 Tag/时间兜底）、创建弹窗提交载荷（username/password/role，
// 默认审核员、可切管理员）与成功刷新、409 重名提示（透出后端 detail、
// 弹窗保持打开且不刷新）。
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Users from './Users'
import { ApiError, apiFetch } from '../api/client'
import type { ManagedUser } from '../api/users'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, apiFetch: vi.fn() }
})

const mockedApiFetch = vi.mocked(apiFetch)

// ---------- 夹具（与后端 UserOut 契约同形，绝无密码字段） ----------

const USERS: ManagedUser[] = [
  {
    user_id: 'u-admin',
    username: 'admin',
    role: 'admin',
    created_at: '2026-10-30T08:00:00',
  },
  {
    user_id: 'u-9',
    username: 'alice',
    role: 'reviewer',
    created_at: null,
  },
]

// ---------- mock 装配与查询辅助 ----------

interface MockOptions {
  users?: ManagedUser[]
  createError?: Error
}

/** 按路径与方法分发的 apiFetch 桩。 */
function mockApis(options: MockOptions = {}) {
  mockedApiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    if (method === 'POST' && path === '/api/v1/users') {
      if (options.createError) throw options.createError
      return { user_id: 'u-new', username: 'bob', role: 'reviewer' }
    }
    if (path === '/api/v1/users') return { items: options.users ?? USERS }
    throw new Error(`未预期的请求：${method} ${path}`)
  })
}

/** 建号端点的请求体（JSON 字符串；未请求过返回 undefined）。 */
function postedCreateBody(): string | undefined {
  const call = mockedApiFetch.mock.calls.find(
    ([p, init]) => p === '/api/v1/users' && (init as RequestInit | undefined)?.method === 'POST',
  )
  return call === undefined ? undefined : String((call[1] as RequestInit).body)
}

/** 账号列表端点的调用次数（成功刷新后递增）。 */
function listCallCount(): number {
  return mockedApiFetch.mock.calls.filter(([p, init]) => p === '/api/v1/users' && (init?.method ?? 'GET') === 'GET').length
}

function renderUsers() {
  return render(<Users />)
}

beforeEach(() => {
  mockedApiFetch.mockReset()
})

afterEach(() => {
  vi.clearAllMocks()
})

// ---------- 用例 ----------

describe('Users 用户管理页', () => {
  it('账号表渲染：用户名/角色 Tag/创建时间与 null 兜底', async () => {
    mockApis()
    renderUsers()

    const row1 = (await screen.findByText('admin')).closest('tr')!
    expect(row1).toHaveTextContent('u-admin')
    expect(row1).toHaveTextContent('管理员')
    expect(row1).toHaveTextContent('2026-10-30 08:00:00')
    const row2 = screen.getByText('alice').closest('tr')!
    expect(row2).toHaveTextContent('审核员')
    expect(row2).toHaveTextContent('—')
  })

  it('创建用户：默认审核员角色提交载荷并刷新列表', async () => {
    mockApis()
    renderUsers()
    await screen.findByText('admin')

    fireEvent.click(screen.getByRole('button', { name: '创建用户' }))
    fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'bob' } })
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'secret' } })
    // 弹窗确定按钮为两字"创建"（antd 渲染为"创 建"），与触发按钮区分
    fireEvent.click(screen.getByRole('button', { name: /创\s*建$/ }))

    await waitFor(() => expect(postedCreateBody()).toBeDefined())
    expect(JSON.parse(postedCreateBody()!)).toEqual({
      username: 'bob',
      password: 'secret',
      role: 'reviewer',
    })
    expect(await screen.findByText('已创建用户 bob（审核员）')).toBeInTheDocument()
    // 成功后刷新列表（关弹窗经退出动画完成，jsdom 不触发 transitionend，
    // 与 GenerationTasks 测试同样只以刷新+提示断言提交成功路径）
    await waitFor(() => expect(listCallCount()).toBe(2))
  })

  it('创建用户：切换管理员角色提交', async () => {
    mockApis()
    renderUsers()
    await screen.findByText('admin')

    fireEvent.click(screen.getByRole('button', { name: '创建用户' }))
    fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'root' } })
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'secret' } })
    fireEvent.click(screen.getByRole('radio', { name: '管理员' }))
    fireEvent.click(screen.getByRole('button', { name: /创\s*建$/ }))

    await waitFor(() => expect(postedCreateBody()).toBeDefined())
    expect(JSON.parse(postedCreateBody()!)).toEqual({
      username: 'root',
      password: 'secret',
      role: 'admin',
    })
  })

  it('创建失败（409 重名）：透出后端 detail，弹窗保持打开且不刷新列表', async () => {
    mockApis({ createError: new ApiError(409, '用户名已存在：bob') })
    renderUsers()
    await screen.findByText('admin')

    fireEvent.click(screen.getByRole('button', { name: '创建用户' }))
    fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'bob' } })
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'secret' } })
    fireEvent.click(screen.getByRole('button', { name: /创\s*建$/ }))

    expect(await screen.findByText('用户名已存在：bob')).toBeInTheDocument()
    expect(listCallCount()).toBe(1)
    // 弹窗保持打开（可修正用户名后重试）
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('用户列表加载失败：渲染错误提示（透出错误文案）', async () => {
    mockedApiFetch.mockImplementation(async () => {
      throw new Error('后端不可用')
    })
    renderUsers()

    expect(await screen.findByText('用户列表加载失败')).toBeInTheDocument()
    expect(screen.getByText('后端不可用')).toBeInTheDocument()
  })
})
