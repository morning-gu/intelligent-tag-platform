// 登录页：AntD 表单提交 login；成功跳工作台，失败原位 Alert 提示
// （401 detail 透传，即后端"用户名或密码错误"）。
import { Alert, Button, Card, Form, Input } from 'antd'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { login } from '../api/client'

interface LoginFormValues {
  username: string
  password: string
}

export default function Login() {
  const navigate = useNavigate()
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleFinish(values: LoginFormValues) {
    setError(null)
    setSubmitting(true)
    try {
      await login(values.username, values.password)
      navigate('/', { replace: true })
    } catch (err) {
      // 密码错误/网络异常均原位提示；文案取后端 detail（见 client.readErrorDetail）
      setError(err instanceof Error ? err.message : '登录失败，请稍后重试')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      style={{
        display: 'flex',
        justifyContent: 'center',
        alignItems: 'center',
        minHeight: '100vh',
        background: '#f5f5f5',
      }}
    >
      <Card title="影视智能标签平台" style={{ width: 360 }}>
        {error !== null && (
          <Alert type="error" message={error} showIcon style={{ marginBottom: 16 }} />
        )}
        <Form<LoginFormValues> layout="vertical" onFinish={handleFinish} disabled={submitting}>
          <Form.Item
            name="username"
            label="用户名"
            rules={[{ required: true, message: '请输入用户名' }]}
          >
            <Input placeholder="用户名" autoComplete="username" />
          </Form.Item>
          <Form.Item
            name="password"
            label="密码"
            rules={[{ required: true, message: '请输入密码' }]}
          >
            <Input.Password placeholder="密码" autoComplete="current-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={submitting}>
            登录
          </Button>
        </Form>
      </Card>
    </div>
  )
}
