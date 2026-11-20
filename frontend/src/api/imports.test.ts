// 断言 multipart 上传载荷、导入任务列表、详情及撤销端点的契约。
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
  it('POST /api/v1/import/tasks：body 为含 file 字段的 FormData，响应回 task_id', async () => {
    mockedApiFetch.mockResolvedValue({
      task_id: 'imp-1',
      status: 'processing',
      file_name: 'tags.xlsx',
      created_at: null,
    } as never)

    const file = new File(['fake-xlsx'], 'tags.xlsx')
    const response = await importTags(file)

    expect(mockedApiFetch).toHaveBeenCalledTimes(1)
    const [path, init] = mockedApiFetch.mock.calls[0] as [string, RequestInit]
    expect(path).toBe('/api/v1/import/tasks')
    expect(init.method).toBe('POST')
    expect(init.body).toBeInstanceOf(FormData)
    const form = init.body as FormData
    expect(form.get('file')).toBe(file)
    expect(response.task_id).toBe('imp-1')
    expect(response.status).toBe('processing')
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
