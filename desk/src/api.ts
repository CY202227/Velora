const BASE = "";

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return res.json() as Promise<T>;
}

export type Session = {
  id: string;
  persona_id: string;
  persona_name: string;
  memory_space_uid: string;
  model: string;
  tts_enabled: boolean;
  style_knobs?: Record<string, unknown>;
  warmth: number;
  created_at: string;
  updated_at: string;
};

export type TurnAttachment = {
  path: string;
  name: string;
  bytes: number;
  kind: string;
};

export type Turn = {
  id: string;
  role: string;
  content: string;
  created_at: string;
  source?: string;
  attachments?: TurnAttachment[];
};

export type Reminder = {
  id: string;
  session_id: string;
  note: string;
  due_at: string;
  status: string;
  deliver_text: string | null;
  created_at: string;
  delivered_at: string | null;
  error: string | null;
  schedule_kind?: string;
  cron_expr?: string | null;
  last_run_at?: string | null;
};

export type Settings = {
  llm_base_url: string;
  llm_model: string;
  llm_api_key_set: boolean;
  atom_memory_base_url: string;
  memory_space_uid: string;
  tts_enabled: boolean;
  show_memory_hints: boolean;
  default_warmth: number;
  user_timezone: string;
  quiet_hours_enabled: boolean;
  quiet_hours_start: string;
  quiet_hours_end: string;
  reminders_enabled: boolean;
  preset_id: string;
  start_memory_sidecar: boolean;
  persona_name: string;
  persona_id: string;
  persona_opener?: string | null;
  web_search_enabled?: boolean;
  tavily_api_key_set?: boolean;
  computer_enabled?: boolean;
  skills_dir?: string;
  mcp_enabled?: boolean;
  local_llm_enabled?: boolean;
  local_llm_base_url?: string;
  local_llm_model?: string;
  start_local_llm_sidecar?: boolean;
};

export type LocalLlmStatus = {
  enabled: boolean;
  ready: boolean;
  running: boolean;
  pid: number | null;
  busy: boolean;
  download: {
    label: string;
    bytes_done: number;
    bytes_total: number;
    percent: number | null;
    status: string;
    error: string | null;
  };
  model: string;
  base_url: string;
  gguf_expected_bytes: number;
  manifest: {
    hf_repo: string;
    gguf_filename: string;
    llama_note: string;
  };
};

export type McpServerEntry = {
  active?: boolean;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  cwd?: string;
  url?: string;
  transport?: string;
  type?: string;
  headers?: Record<string, string>;
  [key: string]: unknown;
};

export type SkillsList = {
  count: number;
  skills_dir: string;
  skills: Array<{ name: string; description: string; path: string }>;
};

export type AtomRow = {
  key: string;
  kind: string;
  statement: string;
  status?: string;
  detail?: string;
  updated_at?: string;
  revisions?: Array<{
    seq: number;
    change_reason?: string;
    trigger?: string;
    statement?: string;
  }>;
  evidence?: Array<{
    revision_seq?: number;
    source_id?: string;
    source_kind?: string;
    source_occurred_at?: string;
  }>;
};

export type MemorySummary = {
  total: number;
  headline: string;
  sections: Array<{
    kind: string;
    label: string;
    count: number;
    items: Array<{ key: string; statement: string }>;
  }>;
};

export type DebugStatus = {
  velora: string;
  llm: {
    ok: boolean;
    base_url: string;
    model: string;
    api_key_set: boolean;
    models_sample: unknown;
  };
  memory: {
    ok: boolean;
    base_url: string;
    space_uid: string;
    detail: unknown;
  };
  sessions_recent: Array<{ id: string; updated_at: string; model: string }>;
};

export type McpStatus = {
  enabled: boolean;
  config_path: string;
  tool_count: number;
  servers: Array<{
    name: string;
    ok: boolean;
    tool_count: number;
    error?: string | null;
    transport?: string;
  }>;
};

/** Interpret datetime-local wall time in an IANA zone → UTC ISO. */
export function zonedLocalToUtcIso(dateTimeLocal: string, timeZone: string): string {
  const [datePart, timePart] = dateTimeLocal.split("T");
  if (!datePart || !timePart) throw new Error("invalid datetime-local");
  const [y, mo, d] = datePart.split("-").map(Number);
  const [hh, mm] = timePart.split(":").map(Number);
  let guess = Date.UTC(y, mo - 1, d, hh, mm, 0);
  for (let i = 0; i < 4; i++) {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).formatToParts(new Date(guess));
    const get = (t: string) => Number(parts.find((p) => p.type === t)?.value);
    const asUtc = Date.UTC(get("year"), get("month") - 1, get("day"), get("hour"), get("minute"));
    const target = Date.UTC(y, mo - 1, d, hh, mm);
    guess += target - asUtc;
  }
  return new Date(guess).toISOString();
}

export function formatInTz(iso: string, timeZone: string): string {
  try {
    return new Date(iso).toLocaleString("zh-CN", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  } catch {
    return iso;
  }
}

export function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

export const api = {
  createSession: (body?: { warmth?: number }) =>
    json<Session>("/api/sessions", { method: "POST", body: JSON.stringify(body || {}) }),
  listSessions: () => json<Session[]>("/api/sessions"),
  listTurns: (id: string) => json<Turn[]>(`/api/sessions/${id}/turns`),
  patchSession: (id: string, body: { warmth?: number; tts_enabled?: boolean; model?: string }) =>
    json<Session>(`/api/sessions/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  getSettings: () => json<Settings>("/api/settings"),
  getLocalLlmStatus: () => json<LocalLlmStatus>("/api/local-llm/status"),
  enableLocalLlm: () =>
    json<LocalLlmStatus>("/api/local-llm/enable", { method: "POST", body: "{}" }),
  disableLocalLlm: () =>
    json<LocalLlmStatus>("/api/local-llm/disable", { method: "POST", body: "{}" }),
  cancelLocalLlmDownload: () =>
    json<LocalLlmStatus>("/api/local-llm/cancel-download", {
      method: "POST",
      body: "{}",
    }),
  updateSettings: (
    body: Partial<{
      llm_base_url: string;
      llm_model: string;
      llm_api_key: string;
      tts_enabled: boolean;
      show_memory_hints: boolean;
      atom_memory_base_url: string;
      default_warmth: number;
      user_timezone: string;
      quiet_hours_enabled: boolean;
      quiet_hours_start: string;
      quiet_hours_end: string;
      web_search_enabled: boolean;
      tavily_api_key: string;
      computer_enabled: boolean;
    }>,
  ) => json<Settings>("/api/settings", { method: "PUT", body: JSON.stringify(body) }),
  listReminders: (sessionId: string, status?: string) => {
    const q = new URLSearchParams({ session_id: sessionId });
    if (status) q.set("status", status);
    return json<Reminder[]>(`/api/reminders?${q}`);
  },
  createReminder: (body: {
    session_id: string;
    note: string;
    due_at?: string;
    cron_expr?: string;
  }) => json<Reminder>("/api/reminders", { method: "POST", body: JSON.stringify(body) }),
  cancelReminder: (id: string) =>
    json<Reminder>(`/api/reminders/${id}/cancel`, { method: "POST" }),
  workspaceFileUrl: (sessionId: string, path: string) =>
    `/api/sessions/${encodeURIComponent(sessionId)}/workspace/${path
      .split("/")
      .map(encodeURIComponent)
      .join("/")}`,
  memorySummary: () => json<MemorySummary>("/api/memory/summary"),
  listAtoms: () =>
    json<{ count: number; results: AtomRow[] }>("/api/memory/atoms?page=1&page_size=100"),
  getAtom: (key: string) => json<AtomRow>(`/api/memory/atoms/${encodeURIComponent(key)}`),
  archiveAtom: (key: string) =>
    json(`/api/memory/atoms/${encodeURIComponent(key)}/archive`, { method: "POST" }),
  archiveKind: (kind: string) =>
    json<{ ok: boolean; archived: number; failed: number }>("/api/memory/atoms/archive-kind", {
      method: "POST",
      body: JSON.stringify({ kind }),
    }),
  postCorrection: (text: string) =>
    json("/api/memory/corrections", { method: "POST", body: JSON.stringify({ text }) }),
  consolidate: () => json("/api/memory/consolidate", { method: "POST" }),
  debugStatus: () => json<DebugStatus>("/api/debug/status"),
  mcpStatus: () => json<McpStatus>("/api/mcp/status"),
  mcpServers: () =>
    json<{ config_path: string; mcpServers: Record<string, McpServerEntry> }>(
      "/api/mcp/servers",
    ),
  putMcpServers: (mcpServers: Record<string, McpServerEntry>) =>
    json<McpStatus>("/api/mcp/servers", {
      method: "PUT",
      body: JSON.stringify({ mcpServers }),
    }),
  reloadMcp: () => json<McpStatus>("/api/mcp/reload", { method: "POST" }),
  testMcpServer: (name: string, config: McpServerEntry) =>
    json<{ ok: boolean; name: string; tool_count: number; error?: string; tools?: unknown[] }>(
      "/api/mcp/servers/test",
      { method: "POST", body: JSON.stringify({ name, config }) },
    ),
  listSkills: () => json<SkillsList>("/api/skills"),
};

/** POST + SSE via fetch stream (EventSource cannot POST). */
export async function streamChat(
  sessionId: string,
  text: string,
  handlers: {
    onToken?: (t: string) => void;
    onEvent?: (type: string, data: unknown) => void;
    onError?: (msg: string) => void;
  },
): Promise<void> {
  const res = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, text }),
  });
  if (!res.ok || !res.body) {
    handlers.onError?.(await res.text());
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let eventType = "message";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n");
    buffer = parts.pop() || "";
    for (const line of parts) {
      if (line.startsWith("event:")) {
        eventType = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        const raw = line.slice(5).trim();
        let data: unknown = raw;
        try {
          data = JSON.parse(raw);
        } catch {
          /* keep string */
        }
        handlers.onEvent?.(eventType, data);
        if (eventType === "token" && data && typeof data === "object" && "text" in data) {
          handlers.onToken?.(String((data as { text: string }).text));
        }
        if (eventType === "error" && data && typeof data === "object" && "message" in data) {
          handlers.onError?.(String((data as { message: string }).message));
        }
        eventType = "message";
      }
    }
  }
}
