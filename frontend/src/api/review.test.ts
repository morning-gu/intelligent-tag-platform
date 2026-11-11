// review API 契约测试（Task 10）：mock apiFetch 模块，断言审核列表/筛选项/
// 单条与批量审核/标准标签搜索的端点路径、查询串组装与请求载荷形状，以及
// 毫秒时间戳格式化（mm:ss）。类型形状与后端 review.py / tags.py 契约对齐。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch } from './client'
import {
  batchReviewAssetTags,
  fetchReviewFilters,
  fetchReviewItems,
  fetchTags,
  formatTimestamp,
  reviewAssetTag,
} from './review'

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

describe('fetchReviewItems 查询串组装', () => {
  it('默认待审核与分页参数拼入查询串', async () => {
    mockedApiFetch.mockResolvedValue({ items: [], total: 0, page: 1 } as never)

    await fetchReviewItems({ status: 'pending', page: 1, page_size: 50 })

    expect(mockedApiFetch).toHaveBeenCalledWith(
      '/api/v1/review/items?status=pending&page=1&page_size=50',
    )
  })

  it('全参数：七筛选 + 分页全部按序拼入', async () => {
    mockedApiFetch.mockResolvedValue({ items: [], total: 0, page: 3 } as never)

    await fetchReviewItems({
      status: 'approved',
      confidence_min: 0.2,
      confidence_max: 0.8,
      tag_type: 'mood',
      mode: 'open',
      asset_id: 'a1',
      origin: 'generated',
      page: 3,
      page_size: 20,
    })

    expect(mockedApiFetch).toHaveBeenCalledWith(
      '/api/v1/review/items?status=approved&confidence_min=0.2&confidence_max=0.8'
      + '&tag_type=mood&mode=open&asset_id=a1&origin=generated&page=3&page_size=20',
    )
  })

  it('confidence_min=0 不被当作缺省丢弃（低分区间的合法边界）', async () => {
    mockedApiFetch.mockResolvedValue({ items: [], total: 0, page: 1 } as never)

    await fetchReviewItems({ confidence_min: 0, confidence_max: 0.1 })

    expect(mockedApiFetch).toHaveBeenCalledWith(
      '/api/v1/review/items?confidence_min=0&confidence_max=0.1',
    )
  })

  it('无参数时不带问号（后端 Query 参数全部可选）', async () => {
    mockedApiFetch.mockResolvedValue({ items: [], total: 0, page: 1 } as never)

    await fetchReviewItems()

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/review/items')
  })
})

describe('审核操作与筛选项端点契约', () => {
  it('fetchReviewFilters：GET /api/v1/review/filters', async () => {
    mockedApiFetch.mockResolvedValue({ tag_types: [], statuses: [], modes: [] } as never)

    await fetchReviewFilters()

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/review/filters')
  })

  it('reviewAssetTag approve：POST 行 review 端点，无 payload 字段', async () => {
    mockedApiFetch.mockResolvedValue({ id: 1 } as never)

    await reviewAssetTag(1, 'approve')

    const [path, init] = mockedApiFetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(path).toBe('/api/v1/asset-tags/1/review')
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual({ action: 'approve' })
  })

  it('reviewAssetTag modify：payload 携带修改字段与备注', async () => {
    mockedApiFetch.mockResolvedValue({ id: 1 } as never)

    await reviewAssetTag(7, 'modify', { tag_name: '新名', comment: '修正拼写' })

    const [, init] = mockedApiFetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(JSON.parse(String(init.body))).toEqual({
      action: 'modify',
      payload: { tag_name: '新名', comment: '修正拼写' },
    })
  })

  it('reviewAssetTag merge：payload 携带 merge_to_tag_id', async () => {
    mockedApiFetch.mockResolvedValue({ id: 1 } as never)

    await reviewAssetTag(3, 'merge', { merge_to_tag_id: 'tag-crime' })

    const [, init] = mockedApiFetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(JSON.parse(String(init.body))).toEqual({
      action: 'merge',
      payload: { merge_to_tag_id: 'tag-crime' },
    })
  })

  it('batchReviewAssetTags：POST 批量端点，ids 与 action 入体', async () => {
    mockedApiFetch.mockResolvedValue({ results: [] } as never)

    await batchReviewAssetTags([1, 2], 'reject')

    const [path, init] = mockedApiFetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(path).toBe('/api/v1/asset-tags/batch-review')
    expect(JSON.parse(String(init.body))).toEqual({ ids: [1, 2], action: 'reject' })
  })

  it('fetchTags：q 子串编码进查询串并限流下拉候选数', async () => {
    mockedApiFetch.mockResolvedValue({ items: [] } as never)

    await fetchTags('暴力')

    expect(mockedApiFetch).toHaveBeenCalledWith(
      `/api/v1/tags?limit=20&q=${encodeURIComponent('暴力')}`,
    )
  })
})

describe('formatTimestamp 毫秒 → mm:ss', () => {
  it.each([
    [0, '00:00'],
    [65_000, '01:05'],
    [3_599_000, '59:59'],
    [3_600_000, '60:00'], // 超过一小时：分钟位自然进位（影视长片场景）
    [95_500, '01:35'], // 秒位向下取整（不四舍五入）
  ])('%d ms → %s', (ms, expected) => {
    expect(formatTimestamp(ms)).toBe(expected)
  })
})
