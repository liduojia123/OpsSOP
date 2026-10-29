/**
 * 来源定位地址与登录回跳校验(第四阶段第一批)。
 *
 * 重点:
 * - 只接受受控 source_type(case/sop)与正整数 source_id,不接受任意 URL;
 * - 版本展示只接受正整数;
 * - redirect 仅允许站内绝对路径,拒绝协议相对/反斜杠/登录页自身。
 */
import { describe, expect, it } from 'vitest'
import {
  isValidSourceId,
  sourcePath,
  sourceTagLabel,
  sourceVersionText,
  sourceViewLabel,
} from '../src/sourceLinks'
import { safeRedirect } from '../src/safeRedirect'

describe('sourcePath:受控来源定位', () => {
  it('案例与 SOP 使用与后端 link 一致的站内路径', () => {
    expect(sourcePath('case', 7)).toBe('/cases/7')
    expect(sourcePath('sop', 9)).toBe('/sops/9')
    expect(sourceViewLabel('case')).toBe('查看案例')
    expect(sourceViewLabel('sop')).toBe('查看 SOP')
    expect(sourceTagLabel('case')).toBe('案例')
    expect(sourceTagLabel('sop')).toBe('SOP')
  })

  it('拒绝任意 URL、非法类型与非法 ID', () => {
    expect(sourcePath('https://evil.example', 1)).toBeNull()
    expect(sourcePath('javascript:alert(1)', 1)).toBeNull()
    expect(sourcePath('case', 0)).toBeNull()
    expect(sourcePath('case', -1)).toBeNull()
    expect(sourcePath('case', 1.5)).toBeNull()
    expect(sourcePath('case', '7')).toBeNull()
    expect(sourcePath('case', Number.NaN)).toBeNull()
    expect(sourcePath('case', Number.MAX_SAFE_INTEGER + 1)).toBeNull()
    expect(sourcePath(null, 1)).toBeNull()
  })

  it('source_id 必须是安全正整数', () => {
    expect(isValidSourceId(1)).toBe(true)
    expect(isValidSourceId(0)).toBe(false)
    expect(isValidSourceId('1')).toBe(false)
    expect(isValidSourceId(Infinity)).toBe(false)
  })

  it('版本只展示正整数', () => {
    expect(sourceVersionText(2)).toBe('v2')
    expect(sourceVersionText(0)).toBe('')
    expect(sourceVersionText(-1)).toBe('')
    expect(sourceVersionText('2')).toBe('')
    expect(sourceVersionText(null)).toBe('')
  })
})

describe('safeRedirect:登录回跳只允许站内路径', () => {
  it('接受站内绝对路径', () => {
    expect(safeRedirect('/cases/7')).toBe('/cases/7')
    expect(safeRedirect('/sops/9?tab=history')).toBe('/sops/9?tab=history')
    expect(safeRedirect('/faults/1#records')).toBe('/faults/1#records')
  })

  it('拒绝协议相对地址、反斜杠和登录页自身', () => {
    expect(safeRedirect('//evil.example')).toBeNull()
    expect(safeRedirect('https://evil.example')).toBeNull()
    expect(safeRedirect('/\\evil.example')).toBeNull()
    expect(safeRedirect('/login')).toBeNull()
    expect(safeRedirect('/login?redirect=/cases/1')).toBeNull()
    expect(safeRedirect(['/cases/1'])).toBeNull()
    expect(safeRedirect(null)).toBeNull()
  })
})
