// imports API 契约测试（v2）：mock apiFetch 模块，断言 multipart 上传的
// FormData 载荷（不强设 Content-Type）、导入任务列表/详情/撤销端点的路径
// 形状。类型形状与后端 tags.py（ImportSummary/ImportResponse）与
// imports.py（ImportTaskOut/ImportTaskDetailOut）契约对齐：v1 的
// /import/batches 前缀与 batch_id 字段随表结构 v2 退役，统一 task_id。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch } from './client'
import { fetchImportTask, fetchImportTasks, importTags, revokeImportTask } from './imports'

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
  it('POST /api/v1/tags/import：body 为含 file 字段的 FormData，响应回 task_id', async () => {
    mockedApiFetch.mockResolvedValue({
      task_id: 'imp-1',
      summary: { total: 1, inserted: 1, skipped: 0, issues: [], parse_errors: [] },
    } as never)

    const file = new File(['fake-xlsx'], 'tags.xlsx')
    const response = await importTags(file)

    expect(mockedApiFetch).toHaveBeenCalledTimes(1)
    const [path, init] = mockedApiFetch.mock.calls[0] as [string, RequestInit]
    expect(path).toBe('/api/v1/tags/import')
    expect(init.method).toBe('POST')
    expect(init.body).toBeInstanceOf(FormData)
    const form = init.body as FormData
    expect(form.get('file')).toBe(file)
    expect(response.task_id).toBe('imp-1')
  })
})

describe('导入任务端点契约（v2：batches → tasks）', () => {
  it('fetchImportTasks：GET /api/v1/import/tasks', async () => {
    mockedApiFetch.mockResolvedValue([] as never)

    await fetchImportTasks()

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/import/tasks')
  })

  it('fetchImportTask：GET /api/v1/import/tasks/{task_id}', async () => {
    mockedApiFetch.mockResolvedValue({ task_id: 'imp-9', summary: null } as never)

    await fetchImportTask('imp-9')

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/import/tasks/imp-9')
  })

  it('revokeImportTask：POST /api/v1/import/tasks/{task_id}/revoke', async () => {
    mockedApiFetch.mockResolvedValue({
      task_id: 'imp-9',
      file_name: 'tags.xlsx',
      status: 'revoked',
      created_at: null,
    } as never)

    await revokeImportTask('imp-9')

    const [path, init] = mockedApiFetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(path).toBe('/api/v1/import/tasks/imp-9/revoke')
    expect(init.method).toBe('POST')
  })
})
