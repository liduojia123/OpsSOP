/**
 * 界面主题：signal（默认，3.7 起的浅色信号光）与 classic（3.6 的经典信号档案）。
 *
 * - 主题只影响外观，不改变数据、权限、路由与业务状态；
 * - 选择保存在当前浏览器（localStorage），不写后端；
 * - 通过 <html data-theme="..."> 生效，经典档案的覆盖样式见 styles/themes/classic.css；
 * - 未保存或保存值无效时回落到默认主题，隐私模式下存储不可用也不阻断本次切换。
 */
import { readonly, ref } from 'vue'

export type ThemeName = 'signal' | 'classic'

export interface ThemeOption {
  value: ThemeName
  label: string
  note: string
}

export const THEME_STORAGE_KEY = 'opssop_theme'
export const DEFAULT_THEME: ThemeName = 'signal'

export const THEME_OPTIONS: ThemeOption[] = [
  { value: 'signal', label: '信号光', note: '浅色线条工作空间，当前默认主题' },
  { value: 'classic', label: '经典档案', note: '石墨导航、暖纸底色与深色标题区' },
]

const activeTheme = ref<ThemeName>(DEFAULT_THEME)

/** 当前生效主题（只读，组件中直接用于高亮当前选项）。 */
export const currentTheme = readonly(activeTheme)

export function isThemeName(value: unknown): value is ThemeName {
  return value === 'signal' || value === 'classic'
}

export function themeLabel(theme: ThemeName): string {
  return THEME_OPTIONS.find((option) => option.value === theme)?.label ?? '信号光'
}

/** 读取保存的主题；存储不可用或值无效时使用默认主题。 */
export function readStoredTheme(): ThemeName {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY)
    return isThemeName(stored) ? stored : DEFAULT_THEME
  } catch {
    return DEFAULT_THEME
  }
}

/** 只应用外观并同步内存状态，不写入存储（测试与初始化复用）。 */
export function applyTheme(theme: ThemeName, root: HTMLElement = document.documentElement): ThemeName {
  root.dataset.theme = theme
  // 两套主题都是浅色界面，明确 color-scheme 让表单控件与滚动条保持浅色。
  root.style.colorScheme = 'light'
  activeTheme.value = theme
  return theme
}

/** 应用并记住用户选择；存储失败时仅本次会话生效。 */
export function setTheme(theme: ThemeName): ThemeName {
  applyTheme(theme)
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme)
  } catch {
    // 浏览器禁用存储时不影响当前页面外观
  }
  return theme
}

export function toggleTheme(): ThemeName {
  return setTheme(activeTheme.value === 'classic' ? 'signal' : 'classic')
}

/** 应用启动时恢复上次选择，须在挂载应用前调用。 */
export function initTheme(): ThemeName {
  return applyTheme(readStoredTheme())
}
