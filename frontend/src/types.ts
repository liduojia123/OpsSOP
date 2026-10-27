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
  /** 模型配置版本:绑定分析请求/响应,配置变化后旧报告标记过期 */
  model_config_version: string
}

// ===== AI 分析=====

export type AiAnalysisStatus = 'success' | 'incomplete' | 'failed'

export interface ObservedFact {
  evidence_ids: string[]
  fact: string
}

export interface Hypothesis {
  title: string
  rationale: string
  evidence_ids: string[]
  reference_ids: string[]
  missing_information: string[]
  verification: string
  /** 优先排查 | 次优先 | 证据不足(不使用概率) */
  priority: string
}

export interface NextCheck {
  description: string
  /** 只读检查命令:仅展示与复制,不执行 */
  command: string
  related_hypothesis: string
}

export interface KnowledgeReference {
  ref_id: string
  source_type: string
  source_id: number
  version: number | null
  relevance: string
}

export interface AnalysisReport {
  summary: string
  observed_facts: ObservedFact[]
  suggested_category: string | null
  suggested_tags: string[]
  hypotheses: Hypothesis[]
  missing_information: string[]
  next_checks: NextCheck[]
  references: KnowledgeReference[]
  insufficient_evidence: boolean
}

export interface AiAnalysis {
  id: number
  fault_id: number
  created_by: number
  trigger_type: string
  input_hash: string
  model_config_version: string
  knowledge_version: string
  is_demo: boolean
  status: AiAnalysisStatus
  report: AnalysisReport | null
  incomplete_reasons: string[]
  error_code: string
  error_message: string
  /** 模型接口返回的 usage;缺失为 null(显示未知),不虚构 token/费用 */
  usage: Record<string, number> | null
  call_count: number
  latency_ms: number
  created_at: string
}

export interface AnalyzeResponse {
  analysis: AiAnalysis
  /** 命中缓存复用已有记录 */
  reused: boolean
  input_substance: boolean
}

export interface SearchHit {
  ref_id: string
  source_type: string
  source_id: number
  version: number | null
  title: string
  snippet: string
  link: string
  status: string
  score: number
}

export interface SearchResponse {
  query: string
  hits: SearchHit[]
  knowledge_version: string
  /** 实际检索方式:keyword(关键词) | hybrid(关键词+向量混合) */
  retrieval_mode: string
  /** 向量路径不可用/失败已降级关键词 */
  degraded: boolean
  /** 安全的降级原因(不含密钥/堆栈) */
  degrade_reason: string
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

// ===== AI 生成 SOP 草稿=====

export type SopGenStatus = 'running' | 'success' | 'failed'

/** 一次 AI 生成运行记录(与故障分析历史分开)。usage 缺失为 null(显示「用量未知」)。 */
export interface SopGenerationRun {
  id: number
  case_id: number
  created_by: number
  request_token: string
  attempt: number
  input_hash: string
  model_config_version: string
  is_demo: boolean
  status: SopGenStatus
  sop_id: number | null
  missing_information: string[]
  incomplete_reasons: string[]
  /** 校验后的字段来源映射 [{target, evidence_ids}](P2-4) */
  field_sources: Array<{ target: string; evidence_ids: string[] }>
  error_code: string
  error_message: string
  usage: Record<string, number> | null
  call_count: number
  latency_ms: number
  created_at: string
  updated_at: string
}

export interface SopGenerateAiResponse {
  run: SopGenerationRun
  sop: Sop | null
  reused: boolean
}
