// clear API 模块契约测试（clear-reset Task 3）：五个 DELETE 端点的路径与
// 方法 + 预览端点的 query 拼装（library 走 tags 路径免参、asset 类带
// asset_id）。与后端 orchestration/_shared.py 的 ClearCountsOut 九键同形。
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  clearAllContentTags,
  clearAssetTags,
  clearTagLibrary,
  deleteAllAssets,
  deleteAsset,
  fetchClearPreview,
  type ClearResult,
} from './clear'

function stubFetch(handler: (url: string, init?: RequestInit) => Promise<Response>) {
  const fetchMock = vi.fn(handler)
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** 与后端 ClearResultOut 同形的夹具（九键全量）。 */
const RESULT: ClearResult = {
  ok: true,
  counts: {
    pre_asset_tag: 2,
    asset_tag: 1,
    generation_tasks: 3,
    quality_evaluations: 1,
    asset_build_task: 0,
    media_assets: 0,
    scenes: 0,
    tag_define: 0,
    tag_catalog: 0,
  },
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('clear API：五个清空/删除端点', () => {
  it('clearAssetTags：DELETE /api/v1/assets/{id}/tags', async () => {
    const fetchMock = stubFetch(async () => jsonResponse(200, RESULT))
    const result = await clearAssetTags('a1')
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/assets/a1/tags')
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('DELETE')
    expect(result).toEqual(RESULT)
  })

  it('clearAllContentTags：DELETE /api/v1/content/tags', async () => {
    const fetchMock = stubFetch(async () => jsonResponse(200, RESULT))
    await clearAllContentTags()
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/content/tags')
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('DELETE')
  })

  it('clearTagLibrary：DELETE /api/v1/tags（集合根路径）', async () => {
    const fetchMock = stubFetch(async () => jsonResponse(200, RESULT))
    await clearTagLibrary()
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/tags')
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('DELETE')
  })

  it('deleteAsset：DELETE /api/v1/assets/{id}', async () => {
    const fetchMock = stubFetch(async () => jsonResponse(200, RESULT))
    await deleteAsset('a1')
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/assets/a1')
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('DELETE')
  })

  it('deleteAllAssets：DELETE /api/v1/assets', async () => {
    const fetchMock = stubFetch(async () => jsonResponse(200, RESULT))
    await deleteAllAssets()
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/assets')
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('DELETE')
  })
})

describe('clear API：预览端点 query 拼装', () => {
  it('library scope：GET /api/v1/tags/clear-preview（免参）', async () => {
    const fetchMock = stubFetch(async () =>
      jsonResponse(200, { scope: 'library', counts: RESULT.counts }))
    const preview = await fetchClearPreview('library')
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/tags/clear-preview')
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBeUndefined()
    expect(preview.scope).toBe('library')
  })

  it('asset-tags scope：content 预览带 scope 与 asset_id', async () => {
    const fetchMock = stubFetch(async () =>
      jsonResponse(200, { scope: 'asset-tags', counts: RESULT.counts }))
    await fetchClearPreview('asset-tags', 'a1')
    expect(fetchMock.mock.calls[0]?.[0])
      .toBe('/api/v1/content/clear-preview?scope=asset-tags&asset_id=a1')
  })

  it('all-tags scope：仅 scope 不带 asset_id', async () => {
    const fetchMock = stubFetch(async () =>
      jsonResponse(200, { scope: 'all-tags', counts: RESULT.counts }))
    await fetchClearPreview('all-tags')
    expect(fetchMock.mock.calls[0]?.[0])
      .toBe('/api/v1/content/clear-preview?scope=all-tags')
  })
})
