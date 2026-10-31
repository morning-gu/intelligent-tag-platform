// 主布局：左侧导航（八项菜单，"用户管理"仅 admin 角色可见）+ 顶部用户名与登出。
// 登录用户经路由门禁（RequireAuth）的 Outlet context 注入，本组件只读不请求。
import { Button, Layout as AntLayout, Menu, Space } from 'antd'
import type { MenuProps } from 'antd'
import { Outlet, useLocation, useNavigate, useOutletContext } from 'react-router-dom'
import { logout, type UserBrief } from '../api/client'

/** RequireAuth 经 Outlet context 传给下游页面的登录态。 */
export interface AuthedOutlet {
  user: UserBrief
}

/** 菜单项数组类型（去掉 MenuProps['items'] 的可选外层，便于展开复用）。 */
type MenuItems = NonNullable<MenuProps['items']>

/** 静态菜单项（工作台至质量评估全员可见）；用户管理按 role 追加。 */
const BASE_MENU_ITEMS: MenuItems = [
  { key: '/', label: '工作台' },
  { key: '/review', label: '审核中心' },
  { key: '/assets', label: '内容资产' },
  { key: '/tasks', label: '生产任务' },
  { key: '/imports', label: '标签导入' },
  { key: '/tags', label: '标签库' },
  { key: '/quality', label: '质量评估' },
]

const { Sider, Header, Content } = AntLayout

export default function Layout() {
  const { user } = useOutletContext<AuthedOutlet>()
  const navigate = useNavigate()
  const location = useLocation()

  const items: MenuItems = [
    ...BASE_MENU_ITEMS,
    ...(user.role === 'admin' ? [{ key: '/users', label: '用户管理' }] : []),
  ]

  async function handleLogout() {
    try {
      await logout()
    } finally {
      // 登出接口幂等（仅清 cookie）：无论成败一律回登录页
      navigate('/login', { replace: true })
    }
  }

  return (
    <AntLayout style={{ minHeight: '100vh' }}>
      <Sider>
        <div
          style={{
            color: '#fff',
            padding: '16px',
            fontSize: 16,
            fontWeight: 600,
            whiteSpace: 'nowrap',
          }}
        >
          影视智能标签平台
        </div>
        <Menu
          theme="dark"
          mode="inline"
          items={items}
          selectedKeys={[location.pathname]}
          onClick={(info) => navigate(info.key)}
        />
      </Sider>
      <AntLayout>
        <Header
          style={{
            background: '#fff',
            display: 'flex',
            justifyContent: 'flex-end',
            alignItems: 'center',
            paddingInline: 24,
          }}
        >
          <Space>
            <span>{user.username}</span>
            <Button onClick={handleLogout}>登出</Button>
          </Space>
        </Header>
        <Content style={{ padding: 24 }}>
          <Outlet />
        </Content>
      </AntLayout>
    </AntLayout>
  )
}
