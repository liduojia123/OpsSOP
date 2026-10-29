/**
 * 登录后回跳目标校验:只允许站内绝对路径。
 *
 * 拒绝协议相对地址(//evil.example)、反斜杠变体、登录页自身,避免把
 * redirect 参数变成开放重定向。
 */
export function safeRedirect(raw: unknown): string | null {
  if (typeof raw !== 'string') return null
  const value = raw.trim()
  if (!value.startsWith('/') || value.startsWith('//')) return null
  if (value.includes('\\')) return null
  if (value === '/login' || value.startsWith('/login?') || value.startsWith('/login/')) {
    return null
  }
  return value
}
