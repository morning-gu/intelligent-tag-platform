// catalog API 契约测试（Task 13）：mock apiFetch 模块，断言 /tags 列表
// 查询串组装（type/q/limit/offset 按序、空参省略、0 保留）与详情路径形状。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch } from './client'
import { fetchTagCatalog, fetchTagDetail } from './catalog'

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

describe('fetchTagCatalog 查询串组装', () => {
  it('无参数时不带问号（后端 Query 参数全部可选）', async () => {
    mockedApiFetch.mockResolvedValue({ items: [] } as never)

    await fetchTagCatalog()

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/tags')
  })

  it('type/q/limit/offset 按序拼入查询串', async () => {
    mockedApiFetch.mockResolvedValue({ items: [] } as never)

    await fetchTagCatalog({ type: 'scene', q: '打斗', limit: 21, offset: 20 })

    expect(mockedApiFetch).toHaveBeenCalledWith(
      `/api/v1/tags?type=scene&q=${encodeURIComponent('打斗')}&limit=21&offset=20`,
    )
  })

  it('空字符串参数不拼入；offset=0 为合法边界值显式保留', async () => {
    mockedApiFetch.mockResolvedValue({ items: [] } as never)

    await fetchTagCatalog({ type: undefined, q: '', limit: 21, offset: 0 })

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/tags?limit=21&offset=0')
  })
})

describe('标签详情端点契约', () => {
  it('fetchTagDetail：GET /api/v1/tags/{tag_id}', async () => {
    mockedApiFetch.mockResolvedValue({ tag_id: 't1', aliases: [] } as never)

    await fetchTagDetail('t1')

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/tags/t1')
  })
})
