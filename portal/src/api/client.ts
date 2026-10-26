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
  MessageLogEntry,
  ScriptList,
  Task,
  TaskDraft,
  TaskRun,
  TokenResponse,
  User,
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
  if (init.body !== undefined) headers.set("Content-Type", "application/json");
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
