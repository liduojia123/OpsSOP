/**
 * 来源定位。
 *
 * 只接受后端校验后的受控 source_type / source_id / version 构造站内地址:
 * - 不直接使用模型或接口返回的任意 URL/link 字段作为跳转目标;
 * - 仅允许 case | sop 两种来源和正整数 ID;
 * - 地址格式与后端检索命中 link 一致(/cases/:id、/sops/:id),可直接打开、刷新。
 */

export type SourceKind = 'case' | 'sop'

export function sourceKind(value: unknown): SourceKind | null {
  return value === 'case' || value === 'sop' ? value : null
}

export function isValidSourceId(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0
}

/** 构造站内来源路径;非法类型/ID 返回 null(调用方不渲染跳转控件)。 */
export function sourcePath(sourceType: unknown, sourceId: unknown): string | null {
  const kind = sourceKind(sourceType)
  if (kind === null || !isValidSourceId(sourceId)) return null
  return kind === 'case' ? `/cases/${sourceId}` : `/sops/${sourceId}`
}

/** 操作控件文案:查看案例 / 查看 SOP。 */
export function sourceViewLabel(sourceType: unknown): string {
  return sourceKind(sourceType) === 'sop' ? '查看 SOP' : '查看案例'
}

/** 类型标签文案:案例 / SOP。 */
export function sourceTagLabel(sourceType: unknown): string {
  return sourceKind(sourceType) === 'sop' ? 'SOP' : '案例'
}

/** 版本展示:仅接受正整数版本,其他显示为空。 */
export function sourceVersionText(version: unknown): string {
  return typeof version === 'number' && Number.isSafeInteger(version) && version > 0
    ? `v${version}`
    : ''
}
