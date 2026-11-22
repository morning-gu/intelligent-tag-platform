// 配置管理容器（Task 3 合一）：用户（Users）与模型与 Prompt（ModelConfig）
// 收编为一页两 tab。tab 状态同步 URL query：默认用户（无参数），模型与
// Prompt 为 ?tab=model——深链/刷新不丢状态；非法参数值回落默认 tab。
// 子组件零改动原样收编，本容器不触碰数据装配。路由挂 AdminRoute（仅 admin）。
import { Tabs } from 'antd'
import { useSearchParams } from 'react-router-dom'
import ModelConfig from './ModelConfig'
import Users from './Users'

/** 合法 tab 键白名单（URL 参数→tab 的唯一裁决处）。 */
const TAB_KEYS = ['users', 'model'] as const

/** 从 URL query 解析激活 tab：非法/缺省回落默认"用户"。 */
function resolveTabKey(tabParam: string | null): (typeof TAB_KEYS)[number] {
  return (TAB_KEYS as readonly string[]).includes(tabParam ?? '') ? (tabParam as (typeof TAB_KEYS)[number]) : 'users'
}

export default function ConfigManagement() {
  const [searchParams, setSearchParams] = useSearchParams()
  const activeKey = resolveTabKey(searchParams.get('tab'))

  function handleChange(key: string) {
    // 默认 tab 不携带 query，保持 URL 干净
    setSearchParams(key === 'users' ? {} : { tab: key })
  }

  return (
    <Tabs
      activeKey={activeKey}
      onChange={handleChange}
      items={[
        { key: 'users', label: '用户', children: <Users /> },
        { key: 'model', label: '模型与 Prompt', children: <ModelConfig /> },
      ]}
    />
  )
}
