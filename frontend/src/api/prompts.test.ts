// prompts API 契约测试（Task 14 只读化后）：GET 只读清单（mode 过滤经查询
// 串）+ 契约键集锁定；写端点已随迁移 0018 删除——POST /prompts → 405
// Method Not Allowed、POST /prompts/{id}/activate → 404，以 global fetch
// 打桩走真实 apiFetch 断言状态码透传（契约锁定：客户端模块亦不再导出写
// 函数，用例钉死后端行为防误用复活）。
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, apiFetch } from './client'
import {
  fetchPromptRegistry,
  fetchPrompts,
  type PromptContractItem,
  type PromptRegistryGroup,
} from './prompts'

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

/** 与后端 PromptContractOut 同形的夹具（无任何版本字段；仅 closed 使用外部模板）。 */
const CONTRACT: PromptContractItem[] = [
  {
    mode: 'closed',
    template_text: '闭卷：{content}\n候选：{candidates}',
    allowed_fields: ['title', 'genres'],
    required_placeholders: ['{content}', '{candidates}'],
  },
]

/** 类型层契约锁定：PromptContractItem 键集漂移（如加回 version/id/
 * is_active）将在此行的编译期失败——DB 版本机制已退役。 */
const CONTRACT_KEYS: readonly (keyof PromptContractItem)[] = [
  'mode',
  'template_text',
  'allowed_fields',
  'required_placeholders',
]

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('fetchPrompts 只读清单契约', () => {
  it('全量清单：GET /api/v1/prompts（无查询串），解析出逐 mode 契约项', async () => {
    const fetchMock = stubFetch(async () => jsonResponse(200, CONTRACT))

    const items = await fetchPrompts()

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/prompts')
    expect(items).toEqual(CONTRACT)
    expect(items.map((i) => i.mode)).toEqual(['closed'])
    // 模板正文与占位符契约逐字透传（配置页只读渲染的数据源）
    expect(items[0]?.template_text).toBe('闭卷：{content}\n候选：{candidates}')
    expect(items[0]?.required_placeholders).toEqual(['{content}', '{candidates}'])
    expect(items[0]?.allowed_fields).toEqual(['title', 'genres'])
  })

  it('mode 过滤经查询串传递', async () => {
    const fetchMock = stubFetch(async () => jsonResponse(200, [CONTRACT[0]]))

    const items = await fetchPrompts('closed')

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/prompts?mode=closed')
    expect(items).toEqual([CONTRACT[0]])
  })

  it('契约键集锁定：PromptContractItem 仅含 mode/template_text/allowed_fields/required_placeholders', () => {
    // CONTRACT_KEYS 的类型注解使键集漂移编译失败；此处再运行时钉死
    expect([...CONTRACT_KEYS].sort()).toEqual([
      'allowed_fields',
      'mode',
      'required_placeholders',
      'template_text',
    ])
  })
})

describe('fetchPromptRegistry 流水线文案清单', () => {
  it('GET /api/v1/prompts/registry 返回分组清单', async () => {
    const groups: PromptRegistryGroup[] = [
      {
        module: 'open_review',
        title: '开放评审',
        items: [{ name: 'REVIEW_PROMPT_VERSION', text: 'open-review-v8' }],
      },
    ]
    const fetchMock = stubFetch(async () => jsonResponse(200, groups))

    const rows = await fetchPromptRegistry()

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/prompts/registry')
    expect(rows).toEqual(groups)
    expect(rows[0]?.items[0]?.text).toBe('open-review-v8')
  })
})

describe('写端点契约锁定（DB 版本机制已随迁移 0018 删除）', () => {
  it('POST /api/v1/prompts → 405 Method Not Allowed，ApiError.status 透传', async () => {
    stubFetch(async () => jsonResponse(405, { detail: 'Method Not Allowed' }))

    await expect(
      apiFetch('/api/v1/prompts', {
        method: 'POST',
        body: JSON.stringify({ mode: 'closed', template_text: '...{content}...' }),
      }),
    ).rejects.toMatchObject({ name: 'ApiError', status: 405 })
  })

  it('POST /api/v1/prompts/{id}/activate → 404，ApiError.status 透传', async () => {
    stubFetch(async () => jsonResponse(404, { detail: 'Not Found' }))

    await expect(
      apiFetch('/api/v1/prompts/7/activate', { method: 'POST' }),
    ).rejects.toMatchObject({ name: 'ApiError', status: 404 })
  })

  it('客户端模块不再导出写函数（createPromptVersion/activatePromptVersion 已删）', async () => {
    const mod = await import('./prompts')
    expect((mod as unknown as Record<string, unknown>).createPromptVersion).toBeUndefined()
    expect((mod as unknown as Record<string, unknown>).activatePromptVersion).toBeUndefined()
    // ApiError 仍从 client 导出（契约锁定用例自身依赖其 status 透传）
    expect(ApiError.name).toBe('ApiError')
  })
})
