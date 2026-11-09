// Mirrors app/schemas.py. Kept hand-written and small rather than generated,
// so the portal has no build-time dependency on a running backend.

export type Role = "admin" | "viewer";

export interface TokenResponse {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  email: string;
  role: Role;
}

export interface User {
  id: number;
  email: string;
  role: Role;
  is_active: boolean;
  last_login_at: string | null;
}

export type ReplyMode = "whole" | "stream";

export type AccessMode = "everyone" | "allowlist" | "blocklist";

export interface AccessEntry {
  id: number;
  username: string;
  note: string;
  created_at: string;
}

export interface AccessCheckResult {
  username: string;
  allowed: boolean;
  reason: string;
}

export type ResponderMode = "rules" | "llm";

export interface BotConfig {
  rc_rest_url: string;
  rc_ws_url: string;
  bot_user_id: string | null;
  bot_username: string | null;
  enabled: boolean;
  responder_mode: ResponderMode;
  greeting: string;
  fallback_reply: string;
  reply_in_thread: boolean;
  respond_in_channels: boolean;
  default_timezone: string;
  reply_mode: ReplyMode;
  stream_chunk_chars: number;
  stream_interval_ms: number;
  access_mode: AccessMode;
  config_version: number;
  has_auth_token: boolean;
  has_password: boolean;
  llm_available: boolean;
  llm_model: string | null;
}

export interface BotConfigUpdate extends Partial<Omit<BotConfig, "config_version" | "has_auth_token" | "has_password" | "llm_available" | "llm_model">> {
  bot_auth_token?: string;
  bot_password?: string;
}

export interface LLMConfig {
  enabled: boolean;
  base_url: string;
  model: string;
  system_prompt: string;
  timeout_seconds: number;
  max_tokens: number | null;
  temperature: number | null;
  history_messages: number;
  format_for_rocketchat: boolean;
  /** JSON object sent with quick side jobs; "" for none. */
  quick_extra_body: string;
  has_api_key: boolean;
  configured: boolean;
  default_system_prompt: string;
}

export interface LLMConfigUpdate
  extends Partial<Omit<LLMConfig, "has_api_key" | "configured" | "default_system_prompt">> {
  /** Write-only. "" clears the stored key; omit to keep it. */
  api_key?: string;
}

export interface LLMTestRequest extends Omit<LLMConfigUpdate, "enabled" | "history_messages"> {
  prompt?: string;
}

export interface LLMTestResult {
  ok: boolean;
  message: string;
  reply: string | null;
  duration_ms: number | null;
}

export interface ConnectionStatus {
  configured: boolean;
  connected: boolean;
  bot_user_id: string | null;
  bot_username: string | null;
  rest_url: string | null;
  ws_url: string | null;
  last_error: string | null;
  messages_handled: number;
  scheduler_running: boolean;
  commands_loaded: number;
  integrations_loaded: number;
  tasks_scheduled: number;
  details: Record<string, string>;
}

export interface ConnectionTestResult {
  ok: boolean;
  message: string;
  bot_user_id: string | null;
  bot_username: string | null;
}

export type TriggerType = "keyword" | "regex";
export type MatchMode = "exact" | "contains" | "prefix";
export type ResponseType = "static" | "integration" | "builtin" | "script";

export interface ScriptList {
  directory: string;
  scripts: { name: string; runner: string }[];
}

export interface Command {
  id: number;
  name: string;
  description: string;
  trigger_type: TriggerType;
  pattern: string;
  match_mode: MatchMode;
  case_sensitive: boolean;
  priority: number;
  enabled: boolean;
  show_in_help: boolean;
  response_type: ResponseType;
  static_response: string;
  builtin_name: string | null;
  integration_id: number | null;
  script_name: string | null;
  script_timeout_seconds: number;
  created_at: string;
  updated_at: string;
}

export type CommandDraft = Omit<Command, "id" | "created_at" | "updated_at">;

export interface CommandTestResult {
  matched: boolean;
  command_name: string | null;
  args: string;
  reply: string | null;
  error: string | null;
}

export type AuthType = "none" | "bearer" | "basic" | "api_key";
export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE" | "HEAD";

export interface IntegrationParam {
  id?: number;
  name: string;
  description: string;
  required: boolean;
  default_value: string;
  greedy: boolean;
  position?: number;
}

export interface Integration {
  id: number;
  name: string;
  description: string;
  base_url: string;
  method: HttpMethod;
  path_template: string;
  query_template: string;
  headers_template: string;
  body_template: string;
  reply_template: string;
  auth_type: AuthType;
  auth_username: string | null;
  auth_header_name: string | null;
  timeout_seconds: number;
  enabled: boolean;
  llm_tool: boolean;
  /** Reuse a successful answer this long for the same request; 0 never. */
  cache_seconds: number;
  has_secret: boolean;
  params: IntegrationParam[];
  created_at: string;
  updated_at: string;
}

export type IntegrationDraft = Omit<Integration, "id" | "created_at" | "updated_at" | "has_secret"> & {
  auth_secret?: string;
};

export interface IntegrationTestResult {
  ok: boolean;
  status_code: number | null;
  url: string | null;
  reply: string | null;
  raw: unknown;
  error: string | null;
  duration_ms: number | null;
}

export type ScheduleKind = "daily" | "weekly" | "cron" | "once";
export type ActionType = "message" | "integration";

export interface Task {
  id: number;
  title: string;
  owner_rc_user_id: string | null;
  owner_username: string | null;
  room_id: string | null;
  dm_username: string | null;
  schedule_kind: ScheduleKind;
  time_of_day: string | null;
  days_of_week: string | null;
  cron_expr: string | null;
  run_at: string | null;
  timezone_name: string;
  action_type: ActionType;
  message: string;
  integration_id: number | null;
  integration_args: string;
  enabled: boolean;
  created_via: string;
  next_run_at: string | null;
  last_run_at: string | null;
  last_status: string | null;
  schedule_description: string;
  next_run_local: string;
  created_at: string;
  updated_at: string;
}

export type TaskDraft = Omit<
  Task,
  | "id" | "created_via" | "next_run_at" | "last_run_at" | "last_status"
  | "schedule_description" | "next_run_local" | "created_at" | "updated_at"
>;

export interface TaskRun {
  id: number;
  task_id: number;
  ran_at: string;
  status: string;
  detail: string;
  duration_ms: number | null;
}

export interface MessageLogEntry {
  id: number;
  received_at: string;
  room_id: string;
  room_type: string | null;
  sender_username: string | null;
  text_in: string;
  matched_command: string | null;
  text_out: string;
  status: string;
  error: string | null;
  duration_ms: number | null;
}

export interface AuditLogEntry {
  id: number;
  at: string;
  actor: string;
  action: string;
  entity: string;
  entity_id: string | null;
  detail: string;
}

export interface DashboardStats {
  commands_total: number;
  commands_enabled: number;
  integrations_total: number;
  tasks_total: number;
  tasks_enabled: number;
  messages_today: number;
  errors_today: number;
  connection: ConnectionStatus;
  recent_messages: MessageLogEntry[];
  upcoming_tasks: Task[];
}

export type QueueMode = "followup" | "collect";

export interface AgentConfig {
  ack_reaction: string;
  fast_model: string;
  turn_deadline_seconds: number;
  queue_mode: QueueMode;
  idle_reset_minutes: number;
  summarize_after_messages: number;
  parallel_tools: boolean;
}

export interface Percentiles {
  p50: number | null;
  p95: number | null;
  max: number | null;
}

export interface AgentRun {
  id: number;
  started_at: string;
  trigger: string;
  status: string;
  model: string;
  rounds: number;
  tools: { name: string; ms: number; ok: boolean }[];
  ack_ms: number | null;
  ttft_ms: number | null;
  total_ms: number | null;
  overhead_ms: number | null;
  error: string | null;
  intent: string | null;
  kb: string | null;
}

export interface AgentStats {
  runs: number;
  since: string | null;
  errors: number;
  timeouts: number;
  ack_ms: Percentiles;
  ttft_ms: Percentiles;
  total_ms: Percentiles;
  overhead_ms: Percentiles;
  recent: AgentRun[];
}

// ------------------------------------------------------------------------- //
// Memory, vector search and profiles
// ------------------------------------------------------------------------- //

export type EmbeddingProvider = "" | "openai" | "tei";
export type RerankProvider = "" | "tei" | "cohere";

export interface EmbeddingConfig {
  provider: EmbeddingProvider;
  base_url: string;
  model: string;
  dimensions: number | null;
  query_prefix: string;
  document_prefix: string;
  timeout_seconds: number;
  has_api_key: boolean;
  rerank_provider: RerankProvider;
  rerank_base_url: string;
  rerank_model: string;
  has_rerank_api_key: boolean;
  configured: boolean;
  rerank_configured: boolean;
  vector_db_configured: boolean;
}

export interface EmbeddingConfigUpdate {
  provider?: EmbeddingProvider;
  base_url?: string;
  api_key?: string;
  model?: string;
  dimensions?: number | null;
  query_prefix?: string;
  document_prefix?: string;
  timeout_seconds?: number;
  rerank_provider?: RerankProvider;
  rerank_base_url?: string;
  rerank_api_key?: string;
  rerank_model?: string;
}

export interface EmbeddingTestResult {
  ok: boolean;
  message: string;
  dimensions: number | null;
  embed_ms: number | null;
  rerank_ok: boolean | null;
  rerank_message: string | null;
  rerank_ms: number | null;
}

export interface MemoryConfig {
  enabled: boolean;
  auto_extract: boolean;
  prompt_notes: number;
  min_score: number;
}

export interface MemoryStatus {
  vector_db: {
    configured: boolean;
    reachable: boolean;
    version: string | null;
    error: string | null;
    memory_vectors: number | null;
  };
  embedding_configured: boolean;
  notes: number;
  pending: number;
  semantic: boolean;
  stages: Record<string, number>;
}

export interface MemoryNote {
  id: number;
  scope: string;
  text: string;
  source: "tool" | "auto" | "portal" | string;
  created_by: string | null;
  created_at: string | null;
  score: number | null;
}

export interface MemoryScope {
  scope: string;
  count: number;
  label: string;
}

export interface Profile {
  rc_user_id: string;
  username: string;
  display_name: string;
  language: string;
  timezone: string;
  about: string;
  notes: number;
  updated_at: string | null;
}

export type ProfileUpdate = Partial<Omit<Profile, "rc_user_id" | "notes" | "updated_at">>;

// ------------------------------------------------------------------------- //
// Knowledge base
// ------------------------------------------------------------------------- //

export type IntentMode = "rules" | "model";

export interface KnowledgeConfig {
  enabled: boolean;
  chat_uploads: boolean;
  top_k: number;
  min_score: number;
  intent_mode: IntentMode;
  llm_index: boolean;
  llm_judge: boolean;
  index_model: string;
}

export interface KbDocument {
  id: number;
  scope: string;
  title: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  pages: number | null;
  chunks: number;
  chars: number;
  truncated: boolean;
  source: "portal" | "chat" | string;
  uploaded_by: string | null;
  created_at: string | null;
  stage: "queued" | "indexing" | "indexed" | "basic" | "failed" | string;
  stage_error: string | null;
  sections: number;
  pending: number;
}

export interface KbUploadResult {
  filename: string;
  ok: boolean;
  message: string;
  document: KbDocument | null;
}

export interface KbPassage {
  id: number;
  document_id: number;
  scope: string;
  label: string;
  text: string;
  score: number | null;
}

export interface KbSearchResult {
  intent: "lookup" | "command" | "chat" | string;
  intent_by: string;
  judged: boolean;
  source: string | null;
  passages: KbPassage[];
}

export interface KbStatus {
  documents: number;
  chat_documents: number;
  passages: number;
  pending: number;
  semantic: boolean;
  stages: Record<string, number>;
}

export interface CacheConfig {
  answer_cache: boolean;
  answer_min_similarity: number;
  pinned_min_similarity: number;
  answer_ttl_hours: number;
}

export interface AnswerSource {
  number: number;
  label: string;
}

export interface AnswerEntry {
  id: number;
  kind: "auto" | "pinned";
  question: string;
  alternatives: string[];
  answer: string;
  sources: AnswerSource[];
  hits: number;
  downvotes: number;
  last_hit_at: string | null;
  expires_at: string | null;
  created_by: string | null;
  created_at: string;
  embedded: boolean;
}

export interface AnswerEntryDraft {
  question: string;
  alternatives: string[];
  answer: string;
}

export interface CacheStats {
  saved: number;
  pinned: number;
  served: number;
  lookups: { entries: number; hits: number; misses: number };
  tool_results: { entries: number; hits: number; misses: number };
}

export interface CacheTestResult {
  found: boolean;
  entry_id: number | null;
  kind: string | null;
  matched_by: string | null;
  score: number | null;
  reply: string | null;
}
