// 主布局：左侧导航（七项菜单，"闭环评测"/"配置管理"仅 admin 角色可见；
// "质量评估"已并入工作台、"标签库/导入任务"合一为标签管理、"用户/模型
// 配置"合一为配置管理）+ 顶部用户名与登出。
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

/** 静态菜单项（全员可见，顺序即模块顺序：标签管理→内容管理→内容打标→
 * 审核中心；命名对齐七模块方案）。 */
const BASE_MENU_ITEMS: MenuItems = [
  { key: '/', label: '工作台' },
  { key: '/tags', label: '标签管理' },
  { key: '/assets', label: '内容管理' },
  { key: '/tasks', label: '内容打标' },
  { key: '/review', label: '审核中心' },
]

/** admin 专属菜单项（闭环评测 + 配置管理），按 role 追加在全员项之后。 */
const ADMIN_MENU_ITEMS: MenuItems = [
  { key: '/eval-closed', label: '闭环评测' },
  { key: '/config', label: '配置管理' },
]

const { Sider, Header, Content } = AntLayout

export default function Layout() {
  const { user } = useOutletContext<AuthedOutlet>()
  const navigate = useNavigate()
  const location = useLocation()

  const items: MenuItems = [
    ...BASE_MENU_ITEMS,
    ...(user.role === 'admin' ? ADMIN_MENU_ITEMS : []),
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
          {/* 必须显式转发登录态：react-router v7 的 Outlet 无条件以传入
              context（缺省 undefined）包 Provider，裸 <Outlet /> 会把
              RequireAuth 下发的 context 重置——下游 AdminRoute 的
              useOutletContext 将解构失败（v6 可穿透继承，v7 不行） */}
          <Outlet context={{ user }} />
        </Content>
      </AntLayout>
    </AntLayout>
  )
}
