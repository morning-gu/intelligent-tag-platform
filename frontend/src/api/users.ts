// 用户管理 API（Task 13）：/users 列表与建号（仅 admin）的类型契约与
// 取数函数。类型形状与后端 tagplatform/api/routers/users.py 的 UserOut /
// CreateUserRequest 一致；409 重名 / 422 role 非法 / 403 非 admin 经
// client 的 ApiError 透出后端 detail。
import { apiFetch } from './client'

/** 账号视图（与后端 UserOut 一致，绝不含密码字段）。 */
export interface ManagedUser {
  user_id: string
  username: string
  role: string
  created_at: string | null
}

/** 建号请求体（与后端 CreateUserRequest 一致）：username/password 非空、
 * role 取值 admin|reviewer（之外后端 422）。 */
export interface CreateUserPayload {
  username: string
  password: string
  role: 'admin' | 'reviewer'
}

/** GET /users 响应：items 包裹约定（按 user_id 排序，不含 password_hash）。 */
export interface UserListResponse {
  items: ManagedUser[]
}

/** 全部账号列表（仅 admin，非 admin 后端 403）。 */
export function fetchUsers(): Promise<UserListResponse> {
  return apiFetch<UserListResponse>('/api/v1/users')
}

/** 建号（仅 admin）：重名 409（ApiError.status 识别，调用方提示冲突）。 */
export function createUser(payload: CreateUserPayload): Promise<ManagedUser> {
  return apiFetch<ManagedUser>('/api/v1/users', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}
