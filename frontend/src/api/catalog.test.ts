// catalog API 契约测试（v2 混合树）：mock apiFetch 模块，断言 /tags 列表
// 查询串组装（q/parent_id/root/scope_id/kind/limit/offset；v1 type 随
// tag_type 枚举化退役）、详情与生命周期三端点的路径与方法形状。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch } from './client'
import { activateTag, deactivateTag, deleteTag, fetchTagCatalog, fetchTagDetail } from './catalog'

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

  it('q/kind/limit/offset 按序拼入查询串（kind 替代 v1 type）', async () => {
    mockedApiFetch.mockResolvedValue({ items: [] } as never)

    await fetchTagCatalog({ q: '打斗', kind: 'tag', limit: 21, offset: 20 })

    expect(mockedApiFetch).toHaveBeenCalledWith(
      `/api/v1/tags?q=${encodeURIComponent('打斗')}&kind=tag&limit=21&offset=20`,
    )
  })

  it('空字符串参数不拼入；offset=0 为合法边界值显式保留', async () => {
    mockedApiFetch.mockResolvedValue({ items: [] } as never)

    await fetchTagCatalog({ q: '', kind: undefined, limit: 21, offset: 0 })

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/tags?limit=21&offset=0')
  })

  it('parent_id / scope_id 树定位键各自拼入查询串', async () => {
    mockedApiFetch.mockResolvedValue({ items: [] } as never)

    await fetchTagCatalog({ parent_id: 'dir-1' })
    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/tags?parent_id=dir-1')

    await fetchTagCatalog({ scope_id: 'dir-1' })
    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/tags?scope_id=dir-1')
  })

  it('root=true 显式发送；root=false 与后端默认同值不发送', async () => {
    mockedApiFetch.mockResolvedValue({ items: [] } as never)

    await fetchTagCatalog({ root: true })
    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/tags?root=true')

    await fetchTagCatalog({ root: false })
    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/tags')
  })
})

describe('详情与生命周期端点契约', () => {
  it('fetchTagDetail：GET /api/v1/tags/{node_id}', async () => {
    mockedApiFetch.mockResolvedValue({ id: 't1', kind: 'tag', aliases: [] } as never)

    await fetchTagDetail('t1')

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/tags/t1')
  })

  it('deactivateTag：POST /api/v1/tags/{tag_id}/deactivate', async () => {
    mockedApiFetch.mockResolvedValue({ id: 't1', status: 'deprecated' } as never)

    await deactivateTag('t1')

    const [path, init] = mockedApiFetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(path).toBe('/api/v1/tags/t1/deactivate')
    expect(init.method).toBe('POST')
  })

  it('activateTag：POST /api/v1/tags/{tag_id}/activate', async () => {
    mockedApiFetch.mockResolvedValue({ id: 't1', status: 'active' } as never)

    await activateTag('t1')

    const [path, init] = mockedApiFetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(path).toBe('/api/v1/tags/t1/activate')
    expect(init.method).toBe('POST')
  })

  it('deleteTag：DELETE /api/v1/tags/{tag_id}', async () => {
    mockedApiFetch.mockResolvedValue({ tag_id: 't1', parent_id: null, parent_name: null } as never)

    await deleteTag('t1')

    const [path, init] = mockedApiFetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(path).toBe('/api/v1/tags/t1')
    expect(init.method).toBe('DELETE')
  })
})
