// 时间戳展示格式化（2026-11-19 起）。
// 后端所有 datetime 字段统一输出带 Z 的 UTC ISO-8601（见后端
// tagplatform/api/schemas.py 的 UtcDatetime），new Date() 解析后自动
// 转本地时区展示——修复"UTC 裸时间戳被当本地时间读"的 8 小时误读
// （gen_4845957f9239 排查结论）。无 Z 的字符串 JS 按本地时间解析，
// 与旧行为一致（测试 fixture 的裸格式显示不变）；非法输入原样返回。

/** ISO 时间 → 本地时区 "YYYY-MM-DD HH:mm:ss"；null → "—"。 */
export function formatTime(v: string | null): string {
  if (v === null) return '—'
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return v
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}
