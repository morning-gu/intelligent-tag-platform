// apiFetch 401 拦截与 login/logout/getMe 契约测试。
// fetch 以 vi.stubGlobal 打桩（真实 Response 对象，覆盖 ok/status/json 逻辑）；
// window.location 在 jsdom 为不可直赋属性（LegacyUnforgeable），
// 以 Object.defineProperty 换成可写桩，断言 401 时 href 被置为 /login。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch, getMe, login, logout, UnauthorizedError } from './client'

// 各用例可读写的 location 桩（beforeEach 复位为未跳转基线）
const locationStub = { href: 'http://localhost/' }

function stubFetch(handler: () => Promise<Response>): ReturnType<typeof vi.fn> {
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

beforeEach(() => {
  locationStub.href = 'http://localhost/'
  Object.defineProperty(window, 'location', {
    value: locationStub,
    writable: true,
    configurable: true,
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('apiFetch', () => {
  it('成功响应解析 JSON 并以 credentials:"include" 携带会话 cookie', async () => {
    const fetchMock = stubFetch(async () => jsonResponse(200, { hello: 'world' }))

    const data = await apiFetch<{ hello: string }>('/api/v1/anything')

    expect(data).toEqual({ hello: 'world' })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/anything',
      expect.objectContaining({ credentials: 'include' }),
    )
    // 未发生 401：不触发登录页跳转
    expect(locationStub.href).toBe('http://localhost/')
  })

  it('带 body 的请求自动 JSON 化并补 Content-Type', async () => {
    const fetchMock = stubFetch(async () => jsonResponse(200, { ok: true }))

    await apiFetch('/api/v1/anything', { method: 'POST', body: JSON.stringify({ a: 1 }) })

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(init.method).toBe('POST')
    expect(init.body).toBe(JSON.stringify({ a: 1 }))
    expect((init.headers as Headers).get('Content-Type')).toBe('application/json')
  })

  it('401（非登录请求）：抛 UnauthorizedError 且跳转 /login', async () => {
    stubFetch(async () => jsonResponse(401, { detail: '未登录' }))

    await expect(apiFetch('/api/v1/assets')).rejects.toBeInstanceOf(UnauthorizedError)

    expect(locationStub.href).toBe('/login')
  })

  it('401（登录请求自身）：抛错并透出 detail，但不跳转（由登录页原位提示）', async () => {
    stubFetch(async () => jsonResponse(401, { detail: '用户名或密码错误' }))

    const err = await login('admin', 'wrong-password').catch((e: unknown) => e)

    expect(err).toBeInstanceOf(UnauthorizedError)
    expect((err as UnauthorizedError).message).toBe('用户名或密码错误')
    expect(locationStub.href).toBe('http://localhost/')
  })

  it('非 401 错误：抛 Error 并透出后端 detail 文案', async () => {
    stubFetch(async () => jsonResponse(409, { detail: '名称重复' }))

    await expect(apiFetch('/api/v1/tags')).rejects.toThrow('名称重复')
    expect(locationStub.href).toBe('http://localhost/')
  })
})

describe('login / logout / getMe 契约', () => {
  it('login：POST /api/v1/auth/login，请求体为 {username,password}', async () => {
    const fetchMock = stubFetch(async () =>
      jsonResponse(200, { user_id: 'u1', username: 'admin', role: 'admin' }))

    const me = await login('admin', 'admin123')

    expect(me).toEqual({ user_id: 'u1', username: 'admin', role: 'admin' })
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(path).toBe('/api/v1/auth/login')
    expect(init.method).toBe('POST')
    expect(init.body).toBe(JSON.stringify({ username: 'admin', password: 'admin123' }))
  })

  it('logout：POST /api/v1/auth/logout', async () => {
    const fetchMock = stubFetch(async () => jsonResponse(200, { ok: true }))

    const result = await logout()

    expect(result).toEqual({ ok: true })
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(path).toBe('/api/v1/auth/logout')
    expect(init.method).toBe('POST')
  })

  it('getMe：GET /api/v1/auth/me', async () => {
    const fetchMock = stubFetch(async () =>
      jsonResponse(200, { user_id: 'u2', username: 'alice', role: 'viewer' }))

    const me = await getMe()

    expect(me.username).toBe('alice')
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(path).toBe('/api/v1/auth/me')
    expect(init.method).toBeUndefined() // 未传 method 即默认 GET
  })
})
