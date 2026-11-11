// assets API 契约测试（v2）：mock apiFetch 模块，断言资产列表查询串组装、
// 详情（recognized 识别状态）/内容 JSON（/content 端点）/正式挂载标签
// （AssetMount 形状）三端点的路径形状与响应透传。类型形状与后端
// content.py（AssetOut/AssetDetail/ContentJSON）、generation.py
// （AssetTagOut）契约对齐；v1 的 contexts 端点与摘要随内容合并退役。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch } from './client'
import { fetchAssetContent, fetchAssetDetail, fetchAssets, fetchAssetTags } from './assets'

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

describe('资产详情/内容/挂载端点契约（v2）', () => {
  it('fetchAssetDetail：GET /api/v1/assets/{asset_id}，响应带 recognized', async () => {
    mockedApiFetch.mockResolvedValue({ asset_id: 'a1', recognized: true } as never)

    const detail = await fetchAssetDetail('a1')

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/assets/a1')
    expect(detail).toEqual({ asset_id: 'a1', recognized: true })
  })

  it('fetchAssetContent：GET /api/v1/assets/{asset_id}/content，响应透传 ContentContext', async () => {
    const content = {
      schema_version: '1.0',
      asset: {
        asset_id: 'a1',
        asset_type: 'movie',
        title: '片名',
        duration_ms: 6_000,
        video_uri: null,
        parent_id: null,
      },
      content: { description: '剧情摘要', characters: [], scenes: [] },
    }
    mockedApiFetch.mockResolvedValue(content as never)

    const result = await fetchAssetContent('a1')

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/assets/a1/content')
    expect(result).toBe(content)
  })

  it('fetchAssetTags：GET /api/v1/assets/{asset_id}/tags，响应为 AssetMount 挂载行', async () => {
    const mounts = [
      { id: 1, tag_id: 'tag-1', tag_name: '打斗', origin: 'closed', created_at: null },
    ]
    mockedApiFetch.mockResolvedValue(mounts as never)

    const rows = await fetchAssetTags('a1')

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/assets/a1/tags')
    expect(rows).toBe(mounts)
  })
})
