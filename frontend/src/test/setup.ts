// 测试全局装配：
// 1) 为 vitest 的 expect 扩展 jest-dom 匹配器（toBeInTheDocument 等）；
// 2) 补 jsdom 缺失的 window.matchMedia——antd 响应式布局/栅格依赖之；
// 3) 补 jsdom 缺失的 ResizeObserver——antd Table（@rc-component/
//    resize-observer）挂载时即注册观察，缺失会直接抛错。
import '@testing-library/jest-dom/vitest'
import { vi } from 'vitest'

// antd 的 responsiveObserver 在 jsdom 下调用 matchMedia 会直接抛错，
// 这里补一个"永不匹配"的最小桩（测试不针对断点行为）。
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(), // 旧版监听 API（antd 兼容路径仍在用）
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }),
})

// Table 测量/虚拟化用的 ResizeObserver：测试不针对尺寸变化，空实现即可。
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
Object.defineProperty(window, 'ResizeObserver', {
  writable: true,
  value: ResizeObserverStub,
})
