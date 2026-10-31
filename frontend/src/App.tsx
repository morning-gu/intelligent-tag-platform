// 应用路由装配：/login 公开；其余页面统一套路由门禁（RequireAuth）与主布局。
// Router 由 main.tsx 提供（BrowserRouter）——App 只含 Routes，测试以
// MemoryRouter 注入。工作台/审核中心/质量评估/内容资产（列表+详情）已实现，
// 其余业务页面仍为占位，由后续任务逐个填充。
import { useEffect, useState } from 'react'
import { Navigate, Outlet, Route, Routes, useOutletContext } from 'react-router-dom'
import { getMe, type UserBrief } from './api/client'
import AssetDetail from './pages/AssetDetail'
import AssetList from './pages/AssetList'
import Dashboard from './pages/Dashboard'
import Layout, { type AuthedOutlet } from './pages/Layout'
import Login from './pages/Login'
import Placeholder from './pages/Placeholder'
import Quality from './pages/Quality'
import ReviewCenter from './pages/ReviewCenter'

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
          <Route path="tasks" element={<Placeholder title="生产任务" />} />
          <Route path="imports" element={<Placeholder title="标签导入" />} />
          <Route path="tags" element={<Placeholder title="标签库" />} />
          <Route path="quality" element={<Quality />} />
          <Route element={<AdminRoute />}>
            <Route path="users" element={<Placeholder title="用户管理" />} />
          </Route>
        </Route>
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
