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

export type Turn = {
  id: string;
  role: string;
  content: string;
  created_at: string;
};

export type Settings = {
  llm_base_url: string;
  llm_model: string;
  llm_api_key_set: boolean;
  atom_memory_base_url: string;
  memory_space_uid: string;
  tts_enabled: boolean;
  default_warmth: number;
  preset_id: string;
  start_memory_sidecar: boolean;
  persona_name: string;
  persona_id: string;
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

export const api = {
  createSession: (body?: { warmth?: number }) =>
    json<Session>("/api/sessions", { method: "POST", body: JSON.stringify(body || {}) }),
  listSessions: () => json<Session[]>("/api/sessions"),
  listTurns: (id: string) => json<Turn[]>(`/api/sessions/${id}/turns`),
  patchSession: (id: string, body: { warmth?: number; tts_enabled?: boolean; model?: string }) =>
    json<Session>(`/api/sessions/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  getSettings: () => json<Settings>("/api/settings"),
  updateSettings: (
    body: Partial<{
      llm_base_url: string;
      llm_model: string;
      llm_api_key: string;
      tts_enabled: boolean;
      atom_memory_base_url: string;
      default_warmth: number;
    }>,
  ) => json<Settings>("/api/settings", { method: "PUT", body: JSON.stringify(body) }),
  listAtoms: () =>
    json<{ count: number; results: AtomRow[] }>("/api/memory/atoms?page=1&page_size=100"),
  getAtom: (key: string) => json<AtomRow>(`/api/memory/atoms/${encodeURIComponent(key)}`),
  archiveAtom: (key: string) =>
    json(`/api/memory/atoms/${encodeURIComponent(key)}/archive`, { method: "POST" }),
  postCorrection: (text: string) =>
    json("/api/memory/corrections", { method: "POST", body: JSON.stringify({ text }) }),
  consolidate: () => json("/api/memory/consolidate", { method: "POST" }),
  debugStatus: () => json<DebugStatus>("/api/debug/status"),
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
