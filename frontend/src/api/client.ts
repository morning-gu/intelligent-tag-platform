// API 客户端：统一 fetch 封装。
// 约定：所有请求带 cookie（credentials: "include"，服务端种 HttpOnly 会话
// cookie `tagplatform_session`）；响应 JSON 化；401 全局拦截——除登录请求
// 自身（密码错误属业务失败，由登录页原位提示）外一律跳转 /login。

/** 未登录/会话过期错误（401 拦截抛出；message 取后端 detail 文案）。 */
export class UnauthorizedError extends Error {
  constructor(message = '未登录或会话已过期') {
    super(message)
    this.name = 'UnauthorizedError'
  }
}

/** 非 401 的请求失败错误：携带 HTTP 状态码（审核中心按 409 冲突等状态
 * 分支处理），message 取后端 detail 文案。 */
export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/** 登录态用户摘要（与后端 UserBrief 契约一致，绝不含密码字段）。 */
export interface UserBrief {
  user_id: string
  username: string
  role: string
}

/** 登录端点：该路径自身的 401 不触发全局跳转（见模块注释）。 */
const LOGIN_PATH = '/api/v1/auth/login'

/** 从错误响应体提取 {"detail": "..."} 提示；非 JSON 响应回落为状态码文案。 */
async function readErrorDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown }
    if (typeof body.detail === 'string' && body.detail !== '') return body.detail
  } catch {
    // 响应体非 JSON（如网关 HTML），走默认文案
  }
  return `请求失败（HTTP ${response.status}）`
}

/** 统一请求入口：自动 JSON、携带会话 cookie、401 拦截（跳 /login）。
 * body 为 FormData 时（multipart 上传，如标签 Excel 导入）不设 Content-Type
 * ——浏览器自动补 `multipart/form-data; boundary=...`，强设 application/json
 * 会覆盖 boundary 导致服务端无法解析。 */
export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (
    init?.body !== undefined &&
    !(init.body instanceof FormData) &&
    !headers.has('Content-Type')
  ) {
    headers.set('Content-Type', 'application/json')
  }
  const response = await fetch(path, { ...init, headers, credentials: 'include' })
  if (response.status === 401) {
    if (path !== LOGIN_PATH) {
      // 会话失效：全局跳登录页（浏览器侧导航；单测以 window.location 桩断言）
      window.location.href = '/login'
    }
    throw new UnauthorizedError(await readErrorDetail(response))
  }
  if (!response.ok) {
    throw new ApiError(response.status, await readErrorDetail(response))
  }
  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}

/** 登录：成功后服务端已种会话 cookie，返回用户摘要。 */
export function login(username: string, password: string): Promise<UserBrief> {
  return apiFetch<UserBrief>(LOGIN_PATH, {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })
}

/** 登出：服务端清会话 cookie（幂等）。 */
export function logout(): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>('/api/v1/auth/logout', { method: 'POST' })
}

/** 当前登录用户摘要；未登录/过期 → 401 → UnauthorizedError。 */
export function getMe(): Promise<UserBrief> {
  return apiFetch<UserBrief>('/api/v1/auth/me')
}
