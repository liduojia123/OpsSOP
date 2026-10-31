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
  signature?: string
  avatar?: string
  role: Role
  is_active: boolean
  created_at: string
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
  source?: string
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
  related_case: LinkedCase | null
}

export interface LinkedCase {
  id: number
  title: string
  status: CaseStatus
  updated_at: string
}

export interface LinkedFault {
  id: number
  title: string
  status: FaultStatus
  updated_at: string
}

export interface LinkedSop {
  id: number
  doc_id: string
  version: number
  title: string
  status: SopStatus
  updated_at: string
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

export interface CaseDetail extends Case {
  source_fault: LinkedFault | null
  related_sops: LinkedSop[]
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

export interface SopDetail extends Sop {
  source_case: LinkedCase | null
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
  /** 用户自填了厂商配置时为 true:即使全局 AI 未启用,该用户也能使用自己的 Key */
  user_ai_enabled: boolean
  /** 当前生效的用户厂商 ID(deepseek / minimax / mimo) */
  user_provider: string
  /** 已保存 Key(即使被关闭):用于区分「未配置」与「已关闭」 */
  user_ai_configured: boolean
  /** 是否配置了实时工具(Prometheus / SSH) */
  tools_available: boolean
}

/** 一次 AI 实时工具调用(只读)。 */
export interface AiToolCall {
  id: number
  tool: string
  arguments: Record<string, unknown>
  status: string
  result_excerpt: string
  error: string
  latency_ms: number
  created_at: string
}

// ===== AI 对话 =====

export interface ChatConversation {
  id: number
  user_id: number
  title: string
  created_at: string
  updated_at: string
  username?: string
  message_count?: number
}

export interface ChatToolCall {
  tool: string
  arguments: Record<string, unknown>
  status: string
  result_excerpt: string
  error: string
  latency_ms: number
}

export interface ChatMessageItem {
  id: number
  conversation_id: number
  role: 'user' | 'assistant'
  content: string
  citations: KnowledgeReference[]
  tool_calls: ChatToolCall[]
  notes: string[]
  status: string
  error_code: string
  error_message: string
  usage: Record<string, number> | null
  model_config_version: string
  /** 回答使用的厂商(deepseek/minimax/mimo),全局配置为 global,旧消息为空 */
  model_provider: string
  /** 回答使用的模型名,旧消息为空 */
  model_name: string
  knowledge_version: string
  latency_ms: number
  created_at: string
}

export interface ChatAskResponse {
  user_message: ChatMessageItem
  assistant_message: ChatMessageItem
}

export interface ChatDetail {
  conversation: ChatConversation
  messages: ChatMessageItem[]
  /** limit>0 时为消息总数,否则 0 */
  total_messages: number
  /** 还有比 messages[0] 更早的消息未返回 */
  has_more: boolean
}

/** 「加载更早消息」分页响应 */
export interface ChatMessagesPage {
  messages: ChatMessageItem[]
  total_messages: number
  has_more: boolean
}

// ===== 监控 =====

export interface MonitoringConfig {
  enabled: boolean
  prometheus_url: string
  timeout_seconds: number
  ai_tools_enabled: boolean
  alerts_enabled: boolean
  alert_service_label: string
  alert_owner_id: number | null
  alert_last_sync_at: string | null
  updated_at: string | null
}

export interface AlertItem {
  name: string
  state: string
  severity: string
  instance: string
  service: string
  summary: string
  active_at: string
}

export interface AlertPreview {
  ok: boolean
  enabled: boolean
  items: AlertItem[]
  error: string
}

export interface AlertSyncResult {
  ok: boolean
  skipped: string
  created: Array<{ id: number; title: string; service: string }>
  firing: number
  total: number
  synced_at: string
  error: string
}

export interface MonitoringSeries {
  labels: Record<string, string>
  points: number[][]
}

export interface MonitoringPanel {
  id: number
  title: string
  unit: string
  kind: string
  promql: string
  series: MonitoringSeries[]
  error: string
  truncated: boolean
  scoped?: boolean
}

export interface FaultMetrics {
  enabled: boolean
  mapped: boolean
  fault_id: number
  service: string
  servers: MonitoredServer[]
  instances: string[]
  panels: MonitoringPanel[]
  fetched_at: string
  error: string
}

export interface AuditTimelineItem {
  id: number
  action: string
  actor_name: string
  detail: Record<string, unknown>
  created_at: string
}

export interface NotificationItem {
  id: number
  user_id: number
  kind: string
  title: string
  body: string
  link: string
  entity_type: string
  entity_id: number | null
  read_at: string | null
  created_at: string
}

export interface MonitoringTarget {
  job: string
  instance: string
  host: string
  health: string
  last_error: string
  last_scrape: string
  scrape_duration: number | null
}

export interface MonitoredServer {
  id: number
  name: string
  host: string
  port: number
  username: string
  prometheus_instance: string
  service_names: string[]
  enabled: boolean
  notes: string
  has_key: boolean
  host_key_fingerprint: string
}

export interface SshTemplate {
  id: number
  key: string
  name: string
  command: string
  param_schema: Record<string, {
    required?: boolean
    type?: string
    min?: number
    max?: number
    pattern?: string
    default?: number | string
  }>
  description: string
  enabled: boolean
  builtin: boolean
}

export interface MonitoringOverview {
  enabled: boolean
  config: Record<string, unknown>
  panels: MonitoringPanel[]
  targets: MonitoringTarget[]
  servers: MonitoredServer[]
  fetched_at: string
  error: string
  targets_error: string
}

export interface RunCommandResult {
  server: string
  template: string
  command: string
  stdout: string
  exit_status: number
  truncated: boolean
  latency_ms: number
}

export interface ServerTestResult {
  ok: boolean
  fingerprint: string
  message: string
}

/** 支持的模型厂商(后端注册表)。 */
export interface AiProvider {
  id: string
  name: string
  base_url: string
  alt_base_urls: string[]
  docs_url: string
  default_models: string[]
  key_prefix: string
  notes: string
  /** 当前用户是否已保存该厂商的 Key */
  has_key: boolean
  /** 当前用户选择的模型 */
  model: string
  /** 当前用户覆盖的接口地址 */
  user_base_url: string
}

/** 用户已保存的厂商配置(GET /api/ai/keys,不含 Key)。 */
export interface AiKeyItem {
  provider: string
  name: string
  has_key: boolean
  model: string
  base_url: string
  enabled: boolean
  updated_at: string
}

/** 管理员全局 AI 配置(不含 Key 明文)。 */
export interface AiGlobalConfig {
  enabled: boolean
  provider: string
  base_url: string
  model: string
  has_key: boolean
  updated_at: string | null
}

/** 厂商模型列表中的一项。 */
export interface AiModel {
  id: string
  name: string
  context_window: number | null
  max_output_tokens: number | null
  reasoning_levels: string[]
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
  tool_calls: AiToolCall[]
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

/** 管理员备份文件(平台内置定时备份)。 */
export interface BackupInfo {
  name: string
  created_at: string | null
  size_bytes: number
  sha256: string
  trigger: string
  retention_days: number | null
}

export interface BackupConfig {
  enabled: boolean
  directory: string
  hour: number
  retention_days: number
  audit_retention_days: number
  next_run: string
  /** 最近备份成功/失败状态；备份失败时 /health 仍是 ok，管理员从这里发现异常 */
  last_success_at: string | null
  last_failure_at: string | null
  last_failure_reason: string | null
  backup_age_hours: number | null
}

export interface SopGenerateAiResponse {
  run: SopGenerationRun
  sop: Sop | null
  reused: boolean
}

// ===== 统一检索 =====

export interface UnifiedHit {
  type: 'fault' | 'case' | 'sop' | 'server'
  id: number
  title: string
  status: string
  subtitle: string
  link: string
  updated_at: string
}

export interface UnifiedGroup {
  type: 'fault' | 'case' | 'sop' | 'server'
  total: number
  truncated: boolean
  items: UnifiedHit[]
}

export interface UnifiedSearchResult {
  query: string
  groups: UnifiedGroup[]
}
