// users API 契约测试（Task 13）：mock apiFetch 模块，断言账号列表路径与
// 建号请求体形状（JSON 序列化 {username,password,role}）。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch } from './client'
import { createUser, fetchUsers } from './users'

vi.mock('./client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./client')>()
  return { ...actual, apiFetch: vi.fn() }
})

const mockedApiFetch = vi.mocked(apiFetch)

beforeEach(() => {
  mockedApiFetch.mockReset()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('users 端点契约', () => {
  it('fetchUsers：GET /api/v1/users', async () => {
    mockedApiFetch.mockResolvedValue({ items: [] } as never)

    await fetchUsers()

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/users')
  })

  it('createUser：POST /api/v1/users，请求体为 {username,password,role}', async () => {
    mockedApiFetch.mockResolvedValue({ user_id: 'u9', username: 'bob', role: 'reviewer' } as never)

    await createUser({ username: 'bob', password: 'secret', role: 'reviewer' })

    const [path, init] = mockedApiFetch.mock.calls[0] as [string, RequestInit]
    expect(path).toBe('/api/v1/users')
    expect(init.method).toBe('POST')
    expect(init.body).toBe(JSON.stringify({ username: 'bob', password: 'secret', role: 'reviewer' }))
  })
})
