import type {
  AccessCheckResult,
  AccessEntry,
  AuditLogEntry,
  BotConfig,
  BotConfigUpdate,
  Command,
  CommandDraft,
  CommandTestResult,
  ConnectionStatus,
  ConnectionTestResult,
  DashboardStats,
  Integration,
  IntegrationDraft,
  IntegrationTestResult,
  LLMConfig,
  LLMConfigUpdate,
  LLMTestRequest,
  LLMTestResult,
  MessageLogEntry,
  ScriptList,
  Task,
  TaskDraft,
  TaskRun,
  TokenResponse,
  User,
  AgentConfig,
  AgentStats,
  EmbeddingConfig,
  EmbeddingConfigUpdate,
  EmbeddingTestResult,
  MemoryConfig,
  MemoryNote,
  MemoryScope,
  MemoryStatus,
  Profile,
  ProfileUpdate,
  KbDocument,
  KbSearchResult,
  KbStatus,
  KbUploadResult,
  KnowledgeConfig,
} from "./types";

const TOKEN_KEY = "rcbot.portal.token";

/** Raised for any non-2xx response, carrying the backend's `detail` message. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }

  get isUnauthorized() {
    return this.status === 401;
  }
}

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    // Private browsing or blocked storage: the session just will not persist.
  }
}

/** FastAPI's `detail` is a string for HTTPException and a list for 422. */
function extractDetail(body: unknown, fallback: string): string {
  if (typeof body === "string" && body) return body;
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((item) => {
          if (item && typeof item === "object" && "msg" in item) {
            const loc = "loc" in item && Array.isArray(item.loc) ? item.loc.slice(1).join(".") : "";
            return loc ? `${loc}: ${String(item.msg)}` : String(item.msg);
          }
          return String(item);
        })
        .join("; ");
    }
  }
  return fallback;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  // A FormData body sets its own multipart Content-Type.
  if (init.body !== undefined && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const response = await fetch(path, { ...init, headers });

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let body: unknown = text;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      /* keep the raw text */
    }
  }

  if (!response.ok) {
    throw new ApiError(response.status, extractDetail(body, `HTTP ${response.status}`));
  }
  return body as T;
}

const get = <T>(path: string) => request<T>(path);
const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
const put = <T>(path: string, body: unknown) =>
  request<T>(path, { method: "PUT", body: JSON.stringify(body) });
const del = (path: string) => request<void>(path, { method: "DELETE" });

export const api = {
  login: (email: string, password: string) =>
    post<TokenResponse>("/api/auth/login", { email, password }),
  me: () => get<User>("/api/auth/me"),
  changePassword: (current_password: string, new_password: string) =>
    post<void>("/api/auth/password", { current_password, new_password }),
  listUsers: () => get<User[]>("/api/auth/users"),

  getConfig: () => get<BotConfig>("/api/config"),
  updateConfig: (changes: BotConfigUpdate) => put<BotConfig>("/api/config", changes),
  getStatus: () => get<ConnectionStatus>("/api/config/status"),
  restartBot: () => post<ConnectionStatus>("/api/config/restart"),
  testConnection: (payload: Record<string, string | undefined>) =>
    post<ConnectionTestResult>("/api/config/test", payload),

  getLLM: () => get<LLMConfig>("/api/llm"),
  updateLLM: (changes: LLMConfigUpdate) => put<LLMConfig>("/api/llm", changes),
  testLLM: (payload: LLMTestRequest) => post<LLMTestResult>("/api/llm/test", payload),

  getAgent: () => get<AgentConfig>("/api/agent"),
  updateAgent: (changes: Partial<AgentConfig>) => put<AgentConfig>("/api/agent", changes),
  agentStats: (hours = 24) => get<AgentStats>(`/api/agent/stats?hours=${hours}`),

  getEmbedding: () => get<EmbeddingConfig>("/api/memory/embedding"),
  updateEmbedding: (changes: EmbeddingConfigUpdate) =>
    put<EmbeddingConfig>("/api/memory/embedding", changes),
  testEmbedding: (payload: EmbeddingConfigUpdate) =>
    post<EmbeddingTestResult>("/api/memory/embedding/test", payload),
  getMemoryConfig: () => get<MemoryConfig>("/api/memory/config"),
  updateMemoryConfig: (changes: Partial<MemoryConfig>) =>
    put<MemoryConfig>("/api/memory/config", changes),
  memoryStatus: () => get<MemoryStatus>("/api/memory/status"),
  reindexMemory: () => post<MemoryStatus>("/api/memory/reindex"),
  listNotes: (params: { scope?: string; q?: string; offset?: number; limit?: number } = {}) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== "") query.set(key, String(value));
    }
    return get<{ items: MemoryNote[]; total: number }>(`/api/memory/notes?${query}`);
  },
  searchNotes: (q: string, scopes: string[]) => {
    const query = new URLSearchParams({ q });
    for (const scope of scopes) query.append("scope", scope);
    return get<MemoryNote[]>(`/api/memory/search?${query}`);
  },
  memoryScopes: () => get<MemoryScope[]>("/api/memory/scopes"),
  addNote: (scope: string, text: string) => post<MemoryNote>("/api/memory/notes", { scope, text }),
  updateNote: (id: number, text: string) => put<MemoryNote>(`/api/memory/notes/${id}`, { text }),
  deleteNote: (id: number) => del(`/api/memory/notes/${id}`),
  forgetScope: (scope: string) => del(`/api/memory/scopes/${encodeURIComponent(scope)}`),
  listProfiles: () => get<Profile[]>("/api/memory/profiles"),
  updateProfile: (rcUserId: string, changes: ProfileUpdate) =>
    put<Profile>(`/api/memory/profiles/${encodeURIComponent(rcUserId)}`, changes),
  deleteProfile: (rcUserId: string) => del(`/api/memory/profiles/${encodeURIComponent(rcUserId)}`),

  getKnowledgeConfig: () => get<KnowledgeConfig>("/api/knowledge/config"),
  updateKnowledgeConfig: (changes: Partial<KnowledgeConfig>) =>
    put<KnowledgeConfig>("/api/knowledge/config", changes),
  knowledgeStatus: () => get<KbStatus>("/api/knowledge/status"),
  listDocuments: (source?: "portal" | "chat") =>
    get<KbDocument[]>(`/api/knowledge/documents${source ? `?source=${source}` : ""}`),
  uploadDocuments: (files: File[]) => {
    const form = new FormData();
    for (const file of files) form.append("files", file, file.name);
    return request<KbUploadResult[]>("/api/knowledge/documents", { method: "POST", body: form });
  },
  deleteDocument: (id: number) => del(`/api/knowledge/documents/${id}`),
  reindexKnowledge: () => post<KbStatus>("/api/knowledge/reindex"),
  searchKnowledge: (q: string) =>
    get<KbSearchResult>(`/api/knowledge/search?q=${encodeURIComponent(q)}`),

  listAccess: () => get<AccessEntry[]>("/api/access"),
  addAccess: (usernames: string, note = "") =>
    post<AccessEntry[]>("/api/access", { usernames, note }),
  deleteAccess: (id: number) => del(`/api/access/${id}`),
  checkAccess: (username: string) =>
    get<AccessCheckResult>(`/api/access/check?username=${encodeURIComponent(username)}`),

  listCommands: () => get<Command[]>("/api/commands"),
  listBuiltins: () => get<string[]>("/api/commands/builtins"),
  listScripts: () => get<ScriptList>("/api/commands/scripts"),
  createCommand: (draft: CommandDraft) => post<Command>("/api/commands", draft),
  updateCommand: (id: number, changes: Partial<CommandDraft>) =>
    put<Command>(`/api/commands/${id}`, changes),
  deleteCommand: (id: number) => del(`/api/commands/${id}`),
  testCommand: (text: string, dry_run = false) =>
    post<CommandTestResult>("/api/commands/test", { text, dry_run }),

  listIntegrations: () => get<Integration[]>("/api/integrations"),
  createIntegration: (draft: IntegrationDraft) => post<Integration>("/api/integrations", draft),
  updateIntegration: (id: number, changes: Partial<IntegrationDraft>) =>
    put<Integration>(`/api/integrations/${id}`, changes),
  deleteIntegration: (id: number) => del(`/api/integrations/${id}`),
  testIntegration: (id: number, params: Record<string, string>) =>
    post<IntegrationTestResult>(`/api/integrations/${id}/test`, { params }),

  listTasks: () => get<Task[]>("/api/tasks"),
  createTask: (draft: TaskDraft) => post<Task>("/api/tasks", draft),
  updateTask: (id: number, changes: Partial<TaskDraft>) => put<Task>(`/api/tasks/${id}`, changes),
  deleteTask: (id: number) => del(`/api/tasks/${id}`),
  runTask: (id: number) => post<TaskRun>(`/api/tasks/${id}/run`),
  listTaskRuns: (id: number) => get<TaskRun[]>(`/api/tasks/${id}/runs`),

  messageLogs: (limit = 100) => get<MessageLogEntry[]>(`/api/logs/messages?limit=${limit}`),
  taskRunLogs: (limit = 100) => get<TaskRun[]>(`/api/logs/task-runs?limit=${limit}`),
  auditLogs: (limit = 100) => get<AuditLogEntry[]>(`/api/logs/audit?limit=${limit}`),

  dashboard: () => get<DashboardStats>("/api/dashboard"),
};
