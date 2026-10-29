import type { CaseStatus, FaultStatus, Role, SopStatus } from './types'

export const faultStatusMap: Record<FaultStatus, { label: string; type: string }> = {
  draft: { label: '草稿', type: 'info' },
  open: { label: '处理中', type: 'warning' },
  resolved: { label: '已解决', type: 'success' },
}

export const caseStatusMap: Record<CaseStatus, { label: string; type: string }> = {
  pending_review: { label: '待审核', type: 'warning' },
  approved: { label: '已通过', type: 'success' },
  returned: { label: '已退回', type: 'danger' },
}

export const sopStatusMap: Record<SopStatus, { label: string; type: string }> = {
  draft: { label: '草稿', type: 'info' },
  submitted: { label: '待审核', type: 'warning' },
  returned: { label: '已退回', type: 'danger' },
  published: { label: '已发布', type: 'success' },
  superseded: { label: '历史版本', type: 'info' },
  deprecated: { label: '已废弃', type: 'danger' },
}

export const roleMap: Record<Role, string> = {
  operator: '运维人员',
  reviewer: '审核人员',
  admin: '管理员',
}

export function fmtTime(v: string | null | undefined): string {
  if (!v) return '-'
  return new Date(v).toLocaleString('zh-CN', { hour12: false })
}
