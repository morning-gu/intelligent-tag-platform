// assets API 契约测试（Task 11）：mock apiFetch 模块，断言资产列表查询串
// 组装、详情/内容/资产标签三端点的路径形状。类型形状与后端 content.py /
// generation.py 契约对齐（media_context 退役后没有 contexts 端点）。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch } from './client'
import {
  fetchAssetContent,
  fetchAssetDetail,
  fetchAssets,
  fetchAssetTags,
} from './assets'

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

describe('fetchAssets 查询串组装', () => {
  it('无参数时不带问号（后端 Query 参数全部可选）', async () => {
    mockedApiFetch.mockResolvedValue([] as never)

    await fetchAssets()

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/assets')
  })

  it('asset_type 与 q 均按序拼入查询串', async () => {
    mockedApiFetch.mockResolvedValue([] as never)

    await fetchAssets({ asset_type: 'movie', q: '星际' })

    expect(mockedApiFetch).toHaveBeenCalledWith(
      `/api/v1/assets?asset_type=movie&q=${encodeURIComponent('星际')}`,
    )
  })

  it('空字符串参数不拼入（与 undefined 同视为缺省）', async () => {
    mockedApiFetch.mockResolvedValue([] as never)

    await fetchAssets({ asset_type: undefined, q: '' })

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/assets')
  })
})

describe('资产详情/内容/标签端点契约', () => {
  it('fetchAssetDetail：GET /api/v1/assets/{asset_id}', async () => {
    mockedApiFetch.mockResolvedValue({ asset_id: 'a1' } as never)

    await fetchAssetDetail('a1')

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/assets/a1')
  })

  it('fetchAssetContent：GET /api/v1/assets/{asset_id}/content', async () => {
    mockedApiFetch.mockResolvedValue({ schema_version: '1.0' } as never)

    await fetchAssetContent('a1')

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/assets/a1/content')
  })

  it('fetchAssetTags：GET /api/v1/assets/{asset_id}/tags', async () => {
    mockedApiFetch.mockResolvedValue([] as never)

    await fetchAssetTags('a1')

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/assets/a1/tags')
  })
})
