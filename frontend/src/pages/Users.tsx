// 用户管理页（Task 13，路由层 admin 守卫已保证仅 admin 可达）：
// 账号列表与建号。
// - 账号 Table：GET /users（user_id 排序由后端保证），角色 Tag、创建时间；
//   列表与表单均不出现密码字段（后端 UserOut 绝不含 password_hash）。
// - 创建 Modal：username/password/role（admin|reviewer Radio，默认审核员）
//   → POST /users → 成功提示 + 关闭刷新；409 重名 / 422 role 非法经
//   ApiError 透出后端 detail（弹窗保持打开，便于修正后重试）。
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  Modal,
  Radio,
  Table,
  Tag,
  message,
} from 'antd'
import type { TableProps } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { createUser, fetchUsers, type CreateUserPayload, type ManagedUser } from '../api/users'

/** 角色 Tag 中文展示与颜色（键集与后端 role 取值一致）。 */
const ROLE_LABELS: Record<string, string> = {
  admin: '管理员',
  reviewer: '审核员',
}
const ROLE_COLORS: Record<string, string> = {
  admin: 'purple',
  reviewer: 'blue',
}

/** 创建表单值：与 CreateUserPayload 同形（role 经 Radio 固定两取值）。 */
interface CreateFormValues {
  username: string
  password: string
  role: 'admin' | 'reviewer'
}

/** ISO 时间 → "YYYY-MM-DD HH:mm:ss"；null → "—"。 */
function formatTime(v: string | null): string {
  return v === null ? '—' : v.replace('T', ' ').slice(0, 19)
}

export default function Users() {
  const [users, setUsers] = useState<ManagedUser[]>([])
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)

  // —— 创建弹窗状态 ——
  const [createOpen, setCreateOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [createForm] = Form.useForm<CreateFormValues>()

  const loadUsers = useCallback(async () => {
    setLoading(true)
    try {
      const res = await fetchUsers()
      setUsers(res.items)
      setLoadError(null)
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : '用户列表加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    // 微任务后启动加载（loading 置位在异步边界内，不在 effect 同步置
    // state）；cancelled 丢弃卸载/依赖变更后的过期触发
    let cancelled = false
    queueMicrotask(() => {
      if (!cancelled) void loadUsers()
    })
    return () => {
      cancelled = true
    }
  }, [loadUsers])

  function openCreate() {
    createForm.resetFields()
    setCreateOpen(true)
  }

  async function handleCreateSubmit() {
    let values: CreateFormValues
    try {
      values = await createForm.validateFields()
    } catch {
      return // 必填校验失败（用户名/密码）：字段已原位提示
    }
    setCreating(true)
    try {
      const payload: CreateUserPayload = {
        username: values.username,
        password: values.password,
        role: values.role,
      }
      await createUser(payload)
      message.success(`已创建用户 ${values.username}（${ROLE_LABELS[values.role]}）`)
      setCreateOpen(false)
      await loadUsers()
    } catch (err) {
      // 409 重名 / 422 role 非法 / 403 非 admin：ApiError.message 均透出后端 detail
      message.error(err instanceof Error ? err.message : '创建用户失败')
    } finally {
      setCreating(false)
    }
  }

  const columns: TableProps<ManagedUser>['columns'] = [
    { title: '用户 ID', dataIndex: 'user_id' },
    { title: '用户名', dataIndex: 'username' },
    {
      title: '角色',
      dataIndex: 'role',
      render: (v: string) => (
        <Tag color={ROLE_COLORS[v] ?? 'default'}>{ROLE_LABELS[v] ?? v}</Tag>
      ),
    },
    { title: '创建时间', dataIndex: 'created_at', render: formatTime },
  ]

  return (
    <Card
      title="用户管理"
      extra={
        <Button type="primary" onClick={openCreate}>
          创建用户
        </Button>
      }
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        {loadError !== null && (
          <Alert type="error" showIcon message="用户列表加载失败" description={loadError} />
        )}
        <Table<ManagedUser>
          rowKey="user_id"
          columns={columns}
          dataSource={users}
          loading={loading}
          pagination={{ showTotal: (t) => `共 ${t} 条` }}
        />
      </div>
      <Modal
        title="创建用户"
        open={createOpen}
        okText="创建"
        cancelText="取消"
        confirmLoading={creating}
        onOk={() => void handleCreateSubmit()}
        onCancel={() => setCreateOpen(false)}
      >
        <Form form={createForm} layout="vertical">
          <Form.Item
            name="username"
            label="用户名"
            rules={[{ required: true, message: '请输入用户名' }]}
          >
            <Input autoComplete="off" />
          </Form.Item>
          <Form.Item
            name="password"
            label="密码"
            rules={[{ required: true, message: '请输入密码' }]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <Form.Item name="role" label="角色" initialValue="reviewer">
            <Radio.Group
              options={[
                { value: 'reviewer', label: '审核员' },
                { value: 'admin', label: '管理员' },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  )
}
