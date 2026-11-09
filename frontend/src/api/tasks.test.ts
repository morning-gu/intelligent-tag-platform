// 生产任务 API 契约测试（Task 12）：mock apiFetch 模块，断言任务列表查询串
// 组装、详情/创建/重试端点的路径与方法形状。类型形状与后端
// tagplatform/api/routers/generation.py 契约对齐。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch } from './client'
import {
  createGenerationTasks,
  fetchGenerationTask,
  fetchGenerationTasks,
  retryGenerationTask,
} from './tasks'

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

describe('fetchGenerationTasks 查询串组装', () => {
  it('无参数时不带问号（后端 Query 参数全部可选）', async () => {
    mockedApiFetch.mockResolvedValue([] as never)

    await fetchGenerationTasks()

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/generation/tasks')
  })

  it('status/mode/asset_id 按序拼入查询串', async () => {
    mockedApiFetch.mockResolvedValue([] as never)

    await fetchGenerationTasks({ status: 'failed', mode: 'open', asset_id: 'a1' })

    expect(mockedApiFetch).toHaveBeenCalledWith(
      '/api/v1/generation/tasks?status=failed&mode=open&asset_id=a1',
    )
  })

  it('空字符串参数不拼入（与 undefined 同视为缺省）', async () => {
    mockedApiFetch.mockResolvedValue([] as never)

    await fetchGenerationTasks({ status: undefined, mode: '' })

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/generation/tasks')
  })
})

describe('详情/创建/重试端点契约', () => {
  it('fetchGenerationTask：GET /api/v1/generation/tasks/{task_id}', async () => {
    mockedApiFetch.mockResolvedValue({ task_id: 't1', results: [] } as never)

    await fetchGenerationTask('t1')

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/generation/tasks/t1')
  })

  it('createGenerationTasks：POST /api/v1/generation/tasks，载荷 JSON 序列化', async () => {
    mockedApiFetch.mockResolvedValue({ tasks: [] } as never)
    const payload = {
      asset_ids: ['a1', 'a2'],
      mode: 'closed',
      policy: { max_tags: 20, min_confidence: 0.5 },
    }

    await createGenerationTasks(payload)

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/generation/tasks', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  })

  it('retryGenerationTask：POST /api/v1/generation/tasks/{task_id}/retry', async () => {
    mockedApiFetch.mockResolvedValue({ task_id: 't1', status: 'queued' } as never)

    await retryGenerationTask('t1')

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/generation/tasks/t1/retry', {
      method: 'POST',
    })
  })
})
