import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  DEFAULT_THEME,
  THEME_STORAGE_KEY,
  applyTheme,
  currentTheme,
  initTheme,
  isThemeName,
  readStoredTheme,
  setTheme,
  themeLabel,
  toggleTheme,
} from '../src/theme'

const root = () => document.documentElement

beforeEach(() => {
  window.localStorage.clear()
  applyTheme(DEFAULT_THEME)
  vi.restoreAllMocks()
})

describe('界面主题', () => {
  it('默认使用信号光主题并标记在 html 上', () => {
    expect(DEFAULT_THEME).toBe('signal')
    expect(currentTheme.value).toBe('signal')
    expect(root().dataset.theme).toBe('signal')
    expect(themeLabel('classic')).toBe('经典档案')
  })

  it('切换到经典档案时写入 html 与 localStorage', () => {
    setTheme('classic')
    expect(root().dataset.theme).toBe('classic')
    expect(currentTheme.value).toBe('classic')
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('classic')
  })

  it('初始化时恢复已保存的经典主题', () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, 'classic')
    expect(initTheme()).toBe('classic')
    expect(root().dataset.theme).toBe('classic')
    expect(currentTheme.value).toBe('classic')
  })

  it('保存值无效时回落到默认主题', () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, 'sepia')
    expect(readStoredTheme()).toBe('signal')
    expect(initTheme()).toBe('signal')
    expect(root().dataset.theme).toBe('signal')
  })

  it('切换在两个主题之间往返', () => {
    setTheme('signal')
    expect(toggleTheme()).toBe('classic')
    expect(toggleTheme()).toBe('signal')
    expect(currentTheme.value).toBe('signal')
  })

  it('只接受两个已知主题名', () => {
    expect(isThemeName('signal')).toBe(true)
    expect(isThemeName('classic')).toBe(true)
    expect(isThemeName('')).toBe(false)
    expect(isThemeName(null)).toBe(false)
    expect(isThemeName('classic ')).toBe(false)
  })

  it('存储不可用时仍能应用主题且不抛错', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('storage blocked')
    })
    expect(readStoredTheme()).toBe(DEFAULT_THEME)
    expect(setTheme('classic')).toBe('classic')
    expect(root().dataset.theme).toBe('classic')
  })
})
