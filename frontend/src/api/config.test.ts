// config API 契约测试：mock apiFetch 模块，断言只读运行配置端点路径。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch } from './client'
import { fetchRuntimeConfig } from './config'

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

describe('config 端点契约', () => {
  it('fetchRuntimeConfig：GET /api/v1/config', async () => {
    mockedApiFetch.mockResolvedValue({} as never)

    await fetchRuntimeConfig()

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/config')
  })
})
