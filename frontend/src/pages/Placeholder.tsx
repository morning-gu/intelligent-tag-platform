// 占位页：本轮仅建路由骨架，各业务页面由后续任务逐个填充。
import { Card, Empty } from 'antd'

export default function Placeholder({ title }: { title: string }) {
  return (
    <Card title={title}>
      <Empty description="建设中" />
    </Card>
  )
}
