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

export interface BotConfig {
  rc_rest_url: string;
  rc_ws_url: string;
  bot_user_id: string | null;
  bot_username: string | null;
  enabled: boolean;
  responder_mode: string;
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
}

export interface BotConfigUpdate extends Partial<Omit<BotConfig, "config_version" | "has_auth_token" | "has_password">> {
  bot_auth_token?: string;
  bot_password?: string;
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
