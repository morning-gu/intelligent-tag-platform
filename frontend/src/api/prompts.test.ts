// prompts API 契约测试：mock apiFetch 模块，断言列表路径（mode 过滤经查询
// 串）、新建版本请求体形状（{mode,template_text}）与激活路径（POST /{id}/activate）。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch } from './client'
import { activatePromptVersion, createPromptVersion, fetchPrompts } from './prompts'

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

describe('prompts 端点契约', () => {
  it('fetchPrompts：GET /api/v1/prompts（全量，无 mode 过滤）', async () => {
    mockedApiFetch.mockResolvedValue([] as never)

    await fetchPrompts()

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/prompts')
  })

  it('fetchPrompts：mode 过滤经查询串传递', async () => {
    mockedApiFetch.mockResolvedValue([] as never)

    await fetchPrompts('closed')

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/prompts?mode=closed')
  })

  it('createPromptVersion：POST /api/v1/prompts，请求体为 {mode,template_text}', async () => {
    mockedApiFetch.mockResolvedValue({
      id: 3,
      mode: 'closed',
      version: 3,
      is_active: true,
      created_at: '2026-11-01T00:00:00',
    } as never)

    await createPromptVersion({ mode: 'closed', template_text: '...{content}...{candidates}...' })

    const [path, init] = mockedApiFetch.mock.calls[0] as [string, RequestInit]
    expect(path).toBe('/api/v1/prompts')
    expect(init.method).toBe('POST')
    expect(init.body).toBe(
      JSON.stringify({ mode: 'closed', template_text: '...{content}...{candidates}...' }),
    )
  })

  it('activatePromptVersion：POST /api/v1/prompts/{id}/activate', async () => {
    mockedApiFetch.mockResolvedValue({ mode: 'open', version: 1, is_active: true } as never)

    await activatePromptVersion(7)

    const [path, init] = mockedApiFetch.mock.calls[0] as [string, RequestInit]
    expect(path).toBe('/api/v1/prompts/7/activate')
    expect(init.method).toBe('POST')
  })
})
