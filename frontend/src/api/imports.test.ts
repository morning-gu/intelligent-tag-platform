// imports API 契约测试（Task 13）：mock apiFetch 模块，断言 multipart 上传
// 的 FormData 载荷（不强设 Content-Type）、批次列表/详情端点的路径形状。
// 类型形状与后端 tags.py / batches.py 契约对齐。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch } from './client'
import { fetchImportBatch, fetchImportBatches, importTags } from './imports'

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

describe('importTags multipart 上传', () => {
  it('POST /api/v1/tags/import：body 为含 file 字段的 FormData', async () => {
    mockedApiFetch.mockResolvedValue({ batch_id: 'b1', summary: null } as never)

    const file = new File(['fake-xlsx'], 'tags.xlsx')
    await importTags(file)

    expect(mockedApiFetch).toHaveBeenCalledTimes(1)
    const [path, init] = mockedApiFetch.mock.calls[0] as [string, RequestInit]
    expect(path).toBe('/api/v1/tags/import')
    expect(init.method).toBe('POST')
    expect(init.body).toBeInstanceOf(FormData)
    const form = init.body as FormData
    expect(form.get('file')).toBe(file)
  })
})

describe('批次历史端点契约', () => {
  it('fetchImportBatches：GET /api/v1/import/batches', async () => {
    mockedApiFetch.mockResolvedValue([] as never)

    await fetchImportBatches()

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/import/batches')
  })

  it('fetchImportBatch：GET /api/v1/import/batches/{batch_id}', async () => {
    mockedApiFetch.mockResolvedValue({ batch_id: 'b-9', summary: null } as never)

    await fetchImportBatch('b-9')

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/import/batches/b-9')
  })
})
