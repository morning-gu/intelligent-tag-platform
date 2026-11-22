// 应用路由装配：/login 公开；其余页面统一套路由门禁（RequireAuth）与主布局。
// Router 由 main.tsx 提供（BrowserRouter）——App 只含 Routes，测试以
// MemoryRouter 注入。全部业务页面已实现：工作台（总览，吸收质量评估）/
// 审核中心/内容资产（列表+详情）/生产任务/标签导入/标签库/用户管理（admin）/
// 模型配置（admin）/闭环评测（admin）；旧 /quality 入口重定向回工作台。
// 页面按模块归位（Task 14）：tag-management/content-management/generation/
// review/closed-eval/config 七目录（open-eval 占位）；Dashboard/Layout/Login
// 为跨模块外壳页，留在 pages 根。
import { useEffect, useState } from 'react'
import { Navigate, Outlet, Route, Routes, useOutletContext } from 'react-router-dom'
import { getMe, type UserBrief } from './api/client'
import AssetDetail from './pages/content-management/AssetDetail'
import AssetList from './pages/content-management/AssetList'
import ClosedEval from './pages/closed-eval/ClosedEval'
import Dashboard from './pages/Dashboard'
import GenerationTasks from './pages/generation/GenerationTasks'
import Layout, { type AuthedOutlet } from './pages/Layout'
import Login from './pages/Login'
import ModelConfig from './pages/config/ModelConfig'
import ReviewCenter from './pages/review/ReviewCenter'
import TagManagement from './pages/tag-management/TagManagement'
import Users from './pages/config/Users'

type GateState =
  | { phase: 'loading' }
  | { phase: 'denied' }
  | { phase: 'ok'; user: UserBrief }

/** 登录门禁：getMe 成功放行（并经 context 下发用户）；失败跳 /login。 */
function RequireAuth() {
  const [gate, setGate] = useState<GateState>({ phase: 'loading' })

  useEffect(() => {
    // StrictMode 会双调 effect：以 cancelled 丢弃过期请求结果
    let cancelled = false
    getMe().then(
      (user) => {
        if (!cancelled) setGate({ phase: 'ok', user })
      },
      () => {
        if (!cancelled) setGate({ phase: 'denied' })
      },
    )
    return () => {
      cancelled = true
    }
  }, [])

  if (gate.phase === 'denied') {
    return <Navigate to="/login" replace />
  }
  if (gate.phase === 'loading') {
    return null // 首帧等待 /me 结果，避免闪现登录页
  }
  return <Outlet context={{ user: gate.user } satisfies AuthedOutlet} />
}

/** admin 门槛：非 admin 直接访问 /users 时重定向回工作台。 */
function AdminRoute() {
  const { user } = useOutletContext<AuthedOutlet>()
  if (user.role !== 'admin') {
    return <Navigate to="/" replace />
  }
  return <Outlet />
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<RequireAuth />}>
        <Route element={<Layout />}>
          <Route index element={<Dashboard />} />
          <Route path="review" element={<ReviewCenter />} />
          <Route path="assets" element={<AssetList />} />
          <Route path="assets/:assetId" element={<AssetDetail />} />
          <Route path="tasks" element={<GenerationTasks />} />
          {/* 标签管理合一（Task 2）：标签库+导入批次收编为 /tags 双 tab，
              旧 /imports 独立路由重定向保书签 */}
          <Route path="tags" element={<TagManagement />} />
          <Route path="imports" element={<Navigate to="/tags?tab=imports" replace />} />
          {/* 旧 /quality 入口已并入工作台总览，重定向保书签 */}
          <Route path="quality" element={<Navigate to="/" replace />} />
          <Route element={<AdminRoute />}>
            <Route path="users" element={<Users />} />
            <Route path="model-config" element={<ModelConfig />} />
            <Route path="eval-closed" element={<ClosedEval />} />
          </Route>
        </Route>
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
