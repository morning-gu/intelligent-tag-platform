// 标签管理容器（Task 2 合一）：标签库（TagCatalog）与导入批次（TagImports）
// 收编为一页两 tab。tab 状态同步 URL query：默认标签库（无参数），导入批次
// 为 ?tab=imports——深链/刷新不丢状态；非法参数值回落默认 tab。
// 子组件零改动原样收编，本容器不触碰数据装配。
import { Tabs } from 'antd'
import { useSearchParams } from 'react-router-dom'
import TagCatalog from './TagCatalog'
import TagImports from './TagImports'

/** 合法 tab 键白名单（URL 参数→tab 的唯一裁决处）。 */
const TAB_KEYS = ['catalog', 'imports'] as const

/** 从 URL query 解析激活 tab：非法/缺省回落默认"标签库"。 */
function resolveTabKey(tabParam: string | null): (typeof TAB_KEYS)[number] {
  return (TAB_KEYS as readonly string[]).includes(tabParam ?? '') ? (tabParam as (typeof TAB_KEYS)[number]) : 'catalog'
}

export default function TagManagement() {
  const [searchParams, setSearchParams] = useSearchParams()
  const activeKey = resolveTabKey(searchParams.get('tab'))

  function handleChange(key: string) {
    // 默认 tab 不携带 query，保持 URL 干净
    setSearchParams(key === 'catalog' ? {} : { tab: key })
  }

  return (
    <Tabs
      activeKey={activeKey}
      onChange={handleChange}
      items={[
        { key: 'catalog', label: '标签库', children: <TagCatalog /> },
        { key: 'imports', label: '导入批次', children: <TagImports /> },
      ]}
    />
  )
}
