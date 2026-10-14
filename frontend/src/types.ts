export type Role = 'operator' | 'reviewer' | 'admin'
export type FaultStatus = 'draft' | 'open' | 'resolved'
export type CaseStatus = 'pending_review' | 'approved' | 'returned'
export type SopStatus =
  | 'draft'
  | 'submitted'
  | 'returned'
  | 'published'
  | 'superseded'
  | 'deprecated'
export type RecordKind = 'check_result' | 'handling_note'
export type FeedbackKind = 'effective' | 'ineffective' | 'suspected_outdated'

export interface User {
  id: number
  username: string
  display_name: string
  role: Role
  is_active: boolean
  created_at: string
}

/** 下拉选择用的最小用户信息(如 SOP 维护人) */
export interface UserOption {
  id: number
  username: string
  display_name: string
  role: Role
}

export interface Category {
  id: number
  name: string
  description: string
  is_system: boolean
  sort_order: number
}

export interface FaultRecord {
  id: number
  fault_id: number
  kind: RecordKind
  content: string
  created_by: number
  created_at: string
}

export interface Fault {
  id: number
  title: string
  description: string
  occurred_at: string | null
  environment: string
  service: string
  component_version: string
  recent_changes: string
  error_logs: string
  attempted_actions: string
  category_id: number | null
  tags: string[]
  status: FaultStatus
  actual_root_cause: string
  handling_steps: string
  recovery_verification: string
  resolved_at: string | null
  created_by: number
  created_at: string
  updated_at: string
}

export interface FaultDetail extends Fault {
  records: FaultRecord[]
}

export interface Case {
  id: number
  fault_id: number
  title: string
  symptom: string
  environment: string
  service: string
  component_version: string
  category_id: number | null
  tags: string[]
  confirmed_root_cause: string
  handling_steps: string
  recovery_verification: string
  status: CaseStatus
  review_comment: string
  reviewed_by: number | null
  reviewed_at: string | null
  created_by: number
  created_at: string
  updated_at: string
}

export interface Sop {
  id: number
  doc_id: string
  version: number
  title: string
  category_id: number | null
  tags: string[]
  applicable_env: string
  symptom: string
  preconditions: string
  check_steps: string
  handling_steps: string
  recovery_verification: string
  rollback_steps: string
  notes: string
  source_case_id: number | null
  maintainer_id: number | null
  status: SopStatus
  review_comment: string
  reviewed_by: number | null
  reviewed_at: string | null
  created_by: number
  created_at: string
  updated_at: string
  published_at: string | null
}

export interface SopFeedback {
  id: number
  sop_id: number
  kind: FeedbackKind
  comment: string
  created_by: number
  created_at: string
}

export interface CategoryStat {
  category_id: number | null
  category_name: string
  count: number
}

export interface Workbench {
  my_open_faults: Fault[]
  pending_case_reviews: Case[]
  pending_sop_reviews: Sop[]
  recent_sops: Sop[]
  /** 有效 SOP 总数(recent_sops 最多 8 条,不能用列表长度当总数) */
  published_sop_count: number
  category_stats: CategoryStat[]
  my_returned_cases: Case[]
  my_returned_sops: Sop[]
}

export interface AiStatus {
  ai_mode: string
  embedding_mode: string
  local_only: boolean
  message: string
}

export interface AuditEvent {
  id: number
  actor_id: number | null
  action: string
  target_type: string
  target_id: string
  detail: Record<string, unknown>
  created_at: string
}
