/**
 * 界面主题预置：在首屏样式生效前恢复上次选择，避免刷新时先按默认主题渲染再切换。
 * 生产 CSP 为 script-src 'self'，所以用独立文件而不是内联脚本；逻辑与 src/theme.ts 保持一致。
 * 存储不可用或值无效时保持默认主题，不阻断页面。
 */
try {
  var savedTheme = window.localStorage.getItem('opssop_theme')
  document.documentElement.dataset.theme = savedTheme === 'classic' ? 'classic' : 'signal'
  document.documentElement.style.colorScheme = 'light'
} catch (error) {
  document.documentElement.dataset.theme = 'signal'
}
