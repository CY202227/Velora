import { useEffect, useRef, useState } from "react";
import {
  api,
  browserTimezone,
  formatInTz,
  streamChat,
  zonedLocalToUtcIso,
  type AtomRow,
  type DebugStatus,
  type LocalLlmStatus,
  type McpServerEntry,
  type McpStatus,
  type MemorySummary,
  type Persona,
  type Reminder,
  type Session,
  type Settings,
  type SkillsList,
  type Turn,
} from "./api";
import { localeOptions, savedLocale, translate, type Locale } from "./i18n";

type McpRow = { name: string; entry: McpServerEntry };

type Tab = "chat" | "memory" | "persona" | "settings";
type Msg = {
  id?: string;
  role: "user" | "assistant";
  content: string;
  streaming?: boolean;
  source?: string;
  created_at?: string;
  attachments?: Array<{ path: string; name: string; bytes: number; kind: string }>;
};
type LogItem = { id: number; t: string; title: string; payload: unknown; open?: boolean };
type RecallItem = {
  key: string;
  statement: string;
  kind: string;
  memory_layer?: number | null;
};

let logSeq = 0;

function isReminderMsg(m: Msg): boolean {
  // Outbound reminder bubbles only (never tag synthetic wake "user" rows).
  if (m.role === "assistant" && m.source === "reminder") return true;
  return (m.content || "").startsWith("【提醒】");
}

function visibleChatMsg(m: Msg): boolean {
  if (m.source === "opener") return false;
  // Legacy rows: hide wake prompts that were persisted as user+reminder.
  if (m.role === "user" && m.source === "reminder") return false;
  return true;
}

/** Display-only: split one turn into visual beats. Storage stays a single message. */
function displayParagraphs(text: string): string[] {
  const parts = text
    .split(/\n{2,}/)
    .map((p) => p.trim())
    .filter(Boolean);
  return parts.length > 1 ? parts : text ? [text] : [];
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function formatMsgTime(iso: string | undefined, timeZone: string, locale: Locale): string | null {
  if (!iso) return null;
  try {
    return new Date(iso).toLocaleString(locale, {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  } catch {
    return iso.slice(0, 16);
  }
}

/** Build 5-field cron from local datetime-local value in user timezone. */
function cronFromLocal(local: string, mode: "daily" | "weekdays" | "weekly"): string {
  const [, time = "09:00"] = local.split("T");
  const [hh = "9", mm = "0"] = time.split(":");
  const minute = String(Number(mm) || 0);
  const hour = String(Number(hh) || 0);
  if (mode === "daily") return `${minute} ${hour} * * *`;
  if (mode === "weekdays") return `${minute} ${hour} * * 1-5`;
  // weekly: use JS day from the picked local date (Sun=0 … Sat=6)
  const datePart = local.split("T")[0] || "";
  const d = new Date(`${datePart}T12:00:00`);
  const dow = Number.isNaN(d.getTime()) ? 1 : d.getDay();
  return `${minute} ${hour} * * ${dow}`;
}

function turnToMsg(t: Turn): Msg {
  return {
    id: t.id,
    role: t.role as "user" | "assistant",
    content: t.content,
    source: t.source || "chat",
    created_at: t.created_at,
    attachments: t.attachments || [],
  };
}

function sessionLabel(s: Session, tz: string, locale: Locale): string {
  const stamp = s.updated_at || s.created_at;
  try {
    return new Date(stamp).toLocaleString(locale, {
      timeZone: tz,
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  } catch {
    return stamp.slice(0, 16);
  }
}

export default function App() {
  const [tab, setTab] = useState<Tab>("chat");
  const [locale, setLocale] = useState<Locale>(savedLocale);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [session, setSession] = useState<Session | null>(null);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [summary, setSummary] = useState<MemorySummary | null>(null);
  const [atomDetail, setAtomDetail] = useState<AtomRow | null>(null);
  const [correction, setCorrection] = useState("");
  const [logs, setLogs] = useState<LogItem[]>([]);
  const [status, setStatus] = useState<DebugStatus | null>(null);
  const [warmth, setWarmth] = useState(35);
  const [memHint, setMemHint] = useState<string | null>(null);
  const [recallItems, setRecallItems] = useState<RecallItem[]>([]);
  const [reminders, setReminders] = useState<Reminder[]>([]);
  const [remNote, setRemNote] = useState("");
  const [remDueLocal, setRemDueLocal] = useState("");
  const [remRepeat, setRemRepeat] = useState<"once" | "daily" | "weekdays" | "weekly" | "custom">(
    "once",
  );
  const [remCron, setRemCron] = useState("");
  const [tzSuggest, setTzSuggest] = useState("");
  const [mcpStatus, setMcpStatus] = useState<McpStatus | null>(null);
  const [mcpRows, setMcpRows] = useState<McpRow[]>([]);
  const [mcpMsg, setMcpMsg] = useState<string | null>(null);
  const [skillsInfo, setSkillsInfo] = useState<SkillsList | null>(null);
  const [debugOn, setDebugOn] = useState(() => localStorage.getItem("velora_debug") === "1");
  const [localLlm, setLocalLlm] = useState<LocalLlmStatus | null>(null);
  const [localLlmBusy, setLocalLlmBusy] = useState(false);
  const [localLlmMsg, setLocalLlmMsg] = useState<string | null>(null);
  const [scheduleOpen, setScheduleOpen] = useState(false);
  const [prefsOpen, setPrefsOpen] = useState(false);
  const [personas, setPersonas] = useState<Persona[]>([]);
  const [personaEditId, setPersonaEditId] = useState<string | null>(null);
  const [personaReadonly, setPersonaReadonly] = useState(false);
  const [personaBook, setPersonaBook] = useState<Record<string, unknown> | null>(null);
  const [personaDraft, setPersonaDraft] = useState({
    name: "",
    description: "",
    personality: "",
    scenario: "",
    opener: "",
    alternate_greetings: "",
    post_history_instructions: "",
    system_prompt: "",
  });
  const [personaMsg, setPersonaMsg] = useState<string | null>(null);
  const [greetingIndex, setGreetingIndex] = useState(0);
  const bottomRef = useRef<HTMLDivElement>(null);
  const warmthTimer = useRef<number | null>(null);
  const lastTurnCount = useRef(0);

  const tz = settings?.user_timezone || "Asia/Shanghai";
  const t = (key: string, variables?: Record<string, string | number>) => translate(locale, key, variables);

  useEffect(() => {
    localStorage.setItem("velora_locale", locale);
    document.documentElement.lang = locale;
  }, [locale]);

  function pushLog(title: string, payload: unknown, open = false) {
    const item: LogItem = {
      id: ++logSeq,
      t: new Date().toLocaleTimeString(),
      title,
      payload,
      open,
    };
    setLogs((xs) => [item, ...xs].slice(0, 80));
  }

  function toggleDebug(on: boolean) {
    setDebugOn(on);
    localStorage.setItem("velora_debug", on ? "1" : "0");
  }

  async function probe() {
    try {
      setStatus(await api.debugStatus());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function loadReminders(sessionId: string) {
    const list = await api.listReminders(sessionId, "pending");
    setReminders(list);
  }

  async function loadPersonas() {
    const list = await api.listPersonas();
    setPersonas(list);
    return list;
  }

  async function selectSession(s: Session, opener?: string | null) {
    setSession(s);
    setWarmth(s.warmth ?? 35);
    setMemHint(null);
    setRecallItems([]);
    const gIdx = s.greeting_index ?? 0;
    setGreetingIndex(gIdx);
    const turns = await api.listTurns(s.id);
    lastTurnCount.current = turns.length;
    let welcome = opener;
    try {
      const p = await api.getPersona(s.persona_id);
      const greets = p.greetings?.length ? p.greetings : p.opener ? [p.opener] : [];
      if (greets.length) {
        welcome = greets[Math.min(gIdx, greets.length - 1)] || greets[0];
      }
    } catch {
      /* keep opener */
    }
    if (turns.length === 0 && welcome) {
      setMsgs([{ role: "assistant", content: welcome, source: "opener" }]);
    } else {
      setMsgs(turns.map(turnToMsg));
    }
    await loadReminders(s.id);
  }

  async function refreshMemory() {
    const sum = await api.memorySummary();
    setSummary(sum);
  }

  useEffect(() => {
    void (async () => {
      try {
        const suggest = browserTimezone();
        setTzSuggest(suggest);
        const s = await api.getSettings();
        setSettings(s);
        await loadPersonas();
        let list = await api.listSessions();
        if (!list.length) {
          const created = await api.createSession({
            warmth: s.default_warmth,
            persona_id: s.default_persona_id,
          });
          list = [created];
        }
        setSessions(list);
        await selectSession(list[0]);
        await probe();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    })();
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [msgs, tab]);

  async function loadMcpEditor() {
    try {
      const [st, cfg, sk] = await Promise.all([
        api.mcpStatus(),
        api.mcpServers(),
        api.listSkills(),
      ]);
      setMcpStatus(st);
      setMcpRows(
        Object.entries(cfg.mcpServers || {}).map(([name, entry]) => ({
          name,
          entry: { active: true, ...entry },
        })),
      );
      setSkillsInfo(sk);
    } catch (e) {
      setMcpStatus(null);
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function refreshLocalLlm() {
    try {
      const st = await api.getLocalLlmStatus();
      setLocalLlm(st);
      setLocalLlmBusy(Boolean(st.busy || st.download?.status === "downloading" || st.download?.status === "extracting"));
      if (st.enabled && st.running) {
        setLocalLlmMsg("本地模型已就绪");
        const s = await api.getSettings();
        setSettings(s);
      }
      if (st.download?.status === "error") {
        setLocalLlmMsg(st.download.error || "本地模型准备失败");
      }
      return st;
    } catch (e) {
      setLocalLlmMsg(e instanceof Error ? e.message : String(e));
      return null;
    }
  }

  async function toggleLocalLlm(on: boolean) {
    setLocalLlmMsg(null);
    try {
      if (on) {
        setLocalLlmBusy(true);
        setLocalLlmMsg("正在准备本地模型（约 2.8GB，可在下方看进度）…");
        const st = await api.enableLocalLlm();
        setLocalLlm(st);
        setLocalLlmBusy(Boolean(st.busy || st.download?.status === "downloading"));
      } else {
        setLocalLlmMsg("正在关闭…");
        const st = await api.disableLocalLlm();
        setLocalLlm(st);
        setLocalLlmBusy(false);
        setLocalLlmMsg("已切回云端配置");
        const s = await api.getSettings();
        setSettings(s);
      }
    } catch (e) {
      setLocalLlmMsg(e instanceof Error ? e.message : String(e));
      setLocalLlmBusy(false);
      void refreshLocalLlm();
    }
  }

  useEffect(() => {
    if (tab === "memory") void refreshMemory().catch((e) => setError(String(e)));
    if (tab === "persona") void loadPersonas().catch((e) => setError(String(e)));
    if (tab === "settings") {
      void probe();
      void loadMcpEditor();
      void refreshLocalLlm();
    }
  }, [tab]);

  useEffect(() => {
    if (tab !== "settings") return;
    const needPoll =
      localLlmBusy ||
      localLlm?.download?.status === "downloading" ||
      (localLlm?.enabled && !localLlm?.ready);
    if (!needPoll) return;
    const timer = window.setInterval(() => {
      void refreshLocalLlm();
    }, 1500);
    return () => window.clearInterval(timer);
  }, [tab, localLlmBusy, localLlm?.download?.status, localLlm?.enabled, localLlm?.ready]);

  // Poll turns while chat tab is open (faster when reminders are pending)
  useEffect(() => {
    if (tab !== "chat" || !session || busy) return;
    const sid = session.id;
    const intervalMs = reminders.length > 0 ? 3000 : 15000;
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          const turns = await api.listTurns(sid);
          if (turns.length <= lastTurnCount.current) return;
          lastTurnCount.current = turns.length;
          setMsgs(turns.map(turnToMsg));
          await loadReminders(sid);
        } catch {
          /* ignore poll errors */
        }
      })();
    }, intervalMs);
    return () => window.clearInterval(timer);
  }, [tab, session?.id, busy, reminders.length]);

  function onWarmthChange(value: number) {
    setWarmth(value);
    if (!session) return;
    if (warmthTimer.current) window.clearTimeout(warmthTimer.current);
    warmthTimer.current = window.setTimeout(() => {
      void api.patchSession(session.id, { warmth: value }).then((s) => {
        setSession(s);
        setSessions((xs) => xs.map((x) => (x.id === s.id ? s : x)));
      });
    }, 300);
  }

  async function send() {
    if (!session || !input.trim() || busy) return;
    const text = input.trim();
    setInput("");
    setBusy(true);
    setError(null);
    setMemHint(null);
    setRecallItems([]);
    pushLog("user_message", { text }, false);
    const sentAt = new Date().toISOString();
    setMsgs((m) => [
      ...m.filter((x) => x.source !== "opener"),
      { role: "user", content: text, created_at: sentAt },
      { role: "assistant", content: "", streaming: true, created_at: sentAt },
    ]);
    let acc = "";
    const hints: string[] = [];
    try {
      await streamChat(session.id, text, {
        onToken: (t) => {
          acc += t;
          setMsgs((m) => {
            const copy = [...m];
            const last = copy[copy.length - 1];
            copy[copy.length - 1] = {
              ...last,
              role: "assistant",
              content: acc,
              streaming: true,
            };
            return copy;
          });
        },
        onEvent: (type, data) => {
          if (type === "token") return;
          pushLog(type, data, type === "error");
          if (type === "assistant_draft_reset") {
            acc = "";
            setMsgs((m) => {
              const copy = [...m];
              const last = copy[copy.length - 1];
              if (last?.role === "assistant" && last.streaming) {
                copy[copy.length - 1] = { ...last, content: "" };
              }
              return copy;
            });
          }
          if (type === "memory_recall" && data && typeof data === "object" && "has_block" in data) {
            const recall = data as { has_block?: boolean; items?: unknown };
            if (recall.has_block) hints.push("本轮用到了长期记忆");
            if (Array.isArray(recall.items)) {
              setRecallItems(
                recall.items.filter(
                  (item): item is RecallItem =>
                    !!item &&
                    typeof item === "object" &&
                    typeof (item as RecallItem).key === "string" &&
                    typeof (item as RecallItem).statement === "string",
                ),
              );
            }
          }
          if (type === "persisted" && data && typeof data === "object" && "memory_wrote" in data) {
            const p = data as { memory_wrote?: boolean; consolidated?: boolean };
            if (p.memory_wrote) {
              hints.push(p.consolidated ? "本轮已写入记忆并固化" : "本轮已写入记忆");
            }
          }
          if (type === "reminder_created") {
            void loadReminders(session.id);
          }
          if (type === "tool_call" && data && typeof data === "object" && "name" in data) {
            hints.push(`调用工具 ${(data as { name: string }).name}`);
          }
          if (type === "tool_result" && data && typeof data === "object" && "name" in data) {
            const n = (data as { name: string }).name;
            if (n === "create_reminder" || n === "list_reminders") {
              void loadReminders(session.id);
            }
          }
        },
        onError: (msg) => setError(msg),
      });
      setMsgs((m) => {
        const copy = [...m];
        const last = copy[copy.length - 1];
        copy[copy.length - 1] = {
          ...last,
          role: "assistant",
          content: acc || last.content,
          streaming: false,
        };
        return copy;
      });
      const turns = await api.listTurns(session.id);
      lastTurnCount.current = turns.length;
      setMsgs(turns.map(turnToMsg));
      if (settings?.show_memory_hints !== false && hints.length) {
        setMemHint([...new Set(hints)].join(" · "));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function newSession() {
    const s = await api.createSession({
      warmth,
      persona_id: session?.persona_id || settings?.default_persona_id,
    });
    setSessions((xs) => [s, ...xs]);
    await selectSession(s);
    pushLog("new_session", s, false);
    await probe();
  }

  async function switchPersona(personaId: string) {
    if (!session) return;
    const s = await api.patchSession(session.id, {
      persona_id: personaId,
      greeting_index: 0,
    });
    setSession(s);
    setSessions((xs) => xs.map((x) => (x.id === s.id ? s : x)));
    setGreetingIndex(0);
    const turns = await api.listTurns(s.id);
    if (turns.length === 0) {
      await selectSession(s);
    }
  }

  async function swipeGreeting(delta: number) {
    if (!session) return;
    const p = personas.find((x) => x.id === session.persona_id) || (await api.getPersona(session.persona_id));
    const greets = p.greetings?.length ? p.greetings : p.opener ? [p.opener] : [];
    if (greets.length < 2) return;
    const next = (greetingIndex + delta + greets.length) % greets.length;
    setGreetingIndex(next);
    await api.patchSession(session.id, { greeting_index: next });
    setMsgs([{ role: "assistant", content: greets[next], source: "opener" }]);
  }

  function startNewPersonaDraft() {
    setPersonaEditId(null);
    setPersonaReadonly(false);
    setPersonaBook(null);
    setPersonaDraft({
      name: "",
      description: "",
      personality: "",
      scenario: "",
      opener: "",
      alternate_greetings: "",
      post_history_instructions: "",
      system_prompt: "",
    });
    setPersonaMsg(null);
  }

  function loadPersonaIntoDraft(p: Persona) {
    setPersonaEditId(p.id);
    setPersonaReadonly(!!p.readonly);
    setPersonaBook((p.character_book as Record<string, unknown> | null) || null);
    setPersonaDraft({
      name: p.name,
      description: p.description || "",
      personality: p.personality || "",
      scenario: p.scenario || "",
      opener: p.opener || "",
      alternate_greetings: (p.alternate_greetings || []).join("\n---\n"),
      post_history_instructions: p.post_history_instructions || "",
      system_prompt: p.system_prompt || "",
    });
    setPersonaMsg(p.readonly ? "内置人格只读；保存将另存为新人格" : null);
  }

  async function savePersonaDraft() {
    const alts = personaDraft.alternate_greetings
      .split(/\n---\n/)
      .map((s) => s.trim())
      .filter(Boolean);
    const body = {
      name: personaDraft.name.trim() || "未命名",
      description: personaDraft.description,
      personality: personaDraft.personality,
      scenario: personaDraft.scenario,
      opener: personaDraft.opener || null,
      alternate_greetings: alts,
      post_history_instructions: personaDraft.post_history_instructions,
      system_prompt: personaDraft.system_prompt || null,
      character_book: personaBook,
    };
    try {
      if (personaEditId && !personaReadonly) {
        const p = await api.updatePersona(personaEditId, body);
        setPersonaMsg(`已保存：${p.name}`);
        setPersonaBook((p.character_book as Record<string, unknown> | null) || null);
      } else {
        const p = await api.createPersona(body);
        setPersonaEditId(p.id);
        setPersonaReadonly(false);
        setPersonaBook((p.character_book as Record<string, unknown> | null) || null);
        setPersonaMsg(`已创建：${p.name}`);
      }
      await loadPersonas();
    } catch (e) {
      setPersonaMsg(e instanceof Error ? e.message : String(e));
    }
  }

  async function openAtom(key: string) {
    try {
      setAtomDetail(await api.getAtom(key));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function createReminder() {
    if (!session || !remNote.trim()) return;
    try {
      if (remRepeat === "once") {
        if (!remDueLocal) return;
        const due_at = zonedLocalToUtcIso(remDueLocal, tz);
        await api.createReminder({ session_id: session.id, note: remNote.trim(), due_at });
      } else if (remRepeat === "custom") {
        const cron = remCron.trim();
        if (!cron) return;
        await api.createReminder({
          session_id: session.id,
          note: remNote.trim(),
          cron_expr: cron,
        });
      } else {
        if (!remDueLocal) return;
        const cron_expr = cronFromLocal(remDueLocal, remRepeat);
        await api.createReminder({
          session_id: session.id,
          note: remNote.trim(),
          cron_expr,
        });
      }
      setRemNote("");
      setRemDueLocal("");
      setRemCron("");
      setRemRepeat("once");
      await loadReminders(session.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function saveSettings(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    const body: Parameters<typeof api.updateSettings>[0] = {
      llm_base_url: String(fd.get("llm_base_url") || ""),
      llm_model: String(fd.get("llm_model") || ""),
      tts_enabled: fd.get("tts_enabled") === "on",
      show_memory_hints: fd.get("show_memory_hints") === "on",
      atom_memory_base_url: String(fd.get("atom_memory_base_url") || ""),
      default_warmth: Number(fd.get("default_warmth") || 35),
      user_timezone: String(fd.get("user_timezone") || tz),
      quiet_hours_enabled: fd.get("quiet_hours_enabled") === "on",
      quiet_hours_start: String(fd.get("quiet_hours_start") || "22:00"),
      quiet_hours_end: String(fd.get("quiet_hours_end") || "08:00"),
      web_search_enabled: fd.get("web_search_enabled") === "on",
      computer_enabled: fd.get("computer_enabled") === "on",
    };
    const key = String(fd.get("llm_api_key") || "");
    if (key) body.llm_api_key = key;
    const tavily = String(fd.get("tavily_api_key") || "");
    if (tavily) body.tavily_api_key = tavily;
    const next = await api.updateSettings(body);
    setSettings(next);
    await probe();
  }

  function updateMcpRow(idx: number, patch: Partial<McpRow> | { entry: Partial<McpServerEntry> }) {
    setMcpRows((rows) =>
      rows.map((r, i) => {
        if (i !== idx) return r;
        if ("entry" in patch && patch.entry) {
          return { ...r, entry: { ...r.entry, ...patch.entry } };
        }
        return { ...r, ...(patch as Partial<McpRow>) };
      }),
    );
  }

  function mcpRowsToMap(): Record<string, McpServerEntry> {
    const out: Record<string, McpServerEntry> = {};
    for (const row of mcpRows) {
      const name = row.name.trim();
      if (!name) continue;
      const e = { ...row.entry };
      if (e.url) {
        delete e.command;
        delete e.args;
      } else {
        delete e.url;
        if (typeof e.args === "string") {
          e.args = String(e.args)
            .split(/\s+/)
            .filter(Boolean);
        }
      }
      out[name] = e;
    }
    return out;
  }

  async function saveMcpAndReload() {
    setMcpMsg(null);
    try {
      const st = await api.putMcpServers(mcpRowsToMap());
      setMcpStatus(st);
      setMcpMsg(`已保存并重载 · 工具 ${st.tool_count}`);
      await loadMcpEditor();
    } catch (e) {
      setMcpMsg(e instanceof Error ? e.message : String(e));
    }
  }

  async function testMcpRow(idx: number) {
    const row = mcpRows[idx];
    if (!row?.name.trim()) return;
    setMcpMsg(null);
    try {
      const r = await api.testMcpServer(row.name.trim(), row.entry);
      setMcpMsg(
        r.ok
          ? `测连 OK · ${r.name} · ${r.tool_count} tools`
          : `测连失败 · ${r.error || "unknown"}`,
      );
    } catch (e) {
      setMcpMsg(e instanceof Error ? e.message : String(e));
    }
  }

  const memBase = status?.memory.base_url || settings?.atom_memory_base_url || "http://127.0.0.1:8020";
  const space = status?.memory.space_uid || settings?.memory_space_uid || "";
  const chatEmpty = msgs.length === 0 || msgs.every((m) => m.source === "opener");
  const activePersona =
    personas.find((p) => p.id === session?.persona_id) || null;
  const greetingCount = activePersona
    ? (activePersona.greetings?.length
        ? activePersona.greetings.length
        : activePersona.opener
          ? 1
          : 0)
    : 0;
  const loreEntries = Array.isArray(personaBook?.entries)
    ? (personaBook.entries as Array<Record<string, unknown>>)
    : [];

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar-brand">
          <span className="brand-name">Velora</span>
          <span className="brand-tag">{t("brand.tag")}</span>
        </div>
        <nav className="topbar-nav">
          <button type="button" className={tab === "chat" ? "active" : ""} onClick={() => setTab("chat")}>
            {t("nav.chat")}
          </button>
          <button type="button" className={tab === "memory" ? "active" : ""} onClick={() => setTab("memory")}>
            {t("nav.memory")}
          </button>
          <button
            type="button"
            className={tab === "persona" ? "active" : ""}
            onClick={() => setTab("persona")}
          >
            {t("nav.persona")}
          </button>
          <button
            type="button"
            className={tab === "settings" ? "active" : ""}
            onClick={() => setTab("settings")}
          >
            {t("nav.settings")}
          </button>
        </nav>
        <div className="topbar-actions">
          <label className="locale-picker">
            <span className="sr-only">{t("language.label")}</span>
            <select value={locale} onChange={(e) => setLocale(e.target.value as Locale)} aria-label={t("language.label")}>
              {localeOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          {tab === "chat" && (
            <>
              <button
                type="button"
                className={`btn ghost ${scheduleOpen ? "active" : ""}`}
                onClick={() => setScheduleOpen(true)}
              >
                {t("schedule")}
              </button>
              <div className="session-menu">
                {activePersona?.has_avatar && (
                  <img
                    className="persona-avatar topbar"
                    src={api.personaAvatarUrl(activePersona.id)}
                    alt=""
                  />
                )}
                <select
                  value={session?.persona_id || ""}
                  onChange={(e) => void switchPersona(e.target.value)}
                  aria-label={t("persona")}
                  title={t("persona.switch")}
                >
                  {personas.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
                <select
                  value={session?.id || ""}
                  onChange={(e) => {
                    const s = sessions.find((x) => x.id === e.target.value);
                    if (s) void selectSession(s);
                  }}
                  aria-label={t("session")}
                >
                  {sessions.map((s) => (
                    <option key={s.id} value={s.id}>
                      {sessionLabel(s, tz, locale)}
                    </option>
                  ))}
                </select>
                <button type="button" className="btn ghost" onClick={() => void newSession()}>
                  {t("newSession")}
                </button>
              </div>
              <button
                type="button"
                className={`btn ghost ${prefsOpen ? "active" : ""}`}
                onClick={() => setPrefsOpen((v) => !v)}
              >
                {t("tone")}
              </button>
            </>
          )}
        </div>
      </header>

      {prefsOpen && tab === "chat" && (
        <div className="prefs-bar">
          <label className="warmth-pick">
            <span>{t("tone.assistant")}</span>
            <input
              type="range"
              min={0}
              max={100}
              value={warmth}
              onChange={(e) => onWarmthChange(Number(e.target.value))}
            />
            <span>{t("tone.warm", { warmth })}</span>
          </label>
        </div>
      )}

      <main className="stage">
        {tab === "chat" && (
          <div className="chat-stage">
            <div className="chat-column">
              {chatEmpty ? (
                <div className="chat-hero">
                  <p className="hero-brand">Velora</p>
                  <h1>{session?.persona_name || settings?.persona_name || t("defaultPersona")}</h1>
                  <p className="sub">{t("chat.subtitle")}</p>
                  {greetingCount > 1 && !!msgs.find((m) => m.source === "opener") && (
                    <div className="greeting-swipe">
                      <button type="button" className="btn ghost" onClick={() => void swipeGreeting(-1)}>
                        {t("greeting.previous")}
                      </button>
                      <span className="sub">
                        {t("greeting.count", { current: greetingIndex + 1, total: greetingCount })}
                      </span>
                      <button type="button" className="btn ghost" onClick={() => void swipeGreeting(1)}>
                        {t("greeting.next")}
                      </button>
                    </div>
                  )}
                  {msgs
                    .filter((m) => m.source === "opener")
                    .map((m, i) => (
                      <p key={i} className="opener-preview">
                        {m.content}
                      </p>
                    ))}
                </div>
              ) : (
                <>
                  <div className="chat-head">
                    <h1>{session?.persona_name || settings?.persona_name || t("defaultPersona")}</h1>
                  </div>
                  {memHint && (
                    <div className="mem-hint">
                      <div className="mem-hint-body">
                        <span>{memHint}</span>
                        {recallItems.length > 0 && (
                          <div className="recall-items">
                            <span className="recall-caption">{t("memory.recalled")}</span>
                            {recallItems.map((item) => (
                              <div className="recall-item" key={item.key}>
                                <span>{item.statement}</span>
                                <div className="recall-actions">
                                  <button
                                    type="button"
                                    className="btn ghost compact"
                                    onClick={() => {
                                      setTab("memory");
                                      void openAtom(item.key);
                                    }}
                                  >
                                    {t("view")}
                                  </button>
                                  <button
                                    type="button"
                                    className="btn ghost compact"
                                    onClick={() => {
                                      setCorrection(`关于「${item.statement}」：实际是 `);
                                      setTab("memory");
                                      void refreshMemory();
                                    }}
                                  >
                                    {t("correct")}
                                  </button>
                                </div>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                      <button type="button" className="btn ghost" onClick={() => setMemHint(null)}>
                        {t("close")}
                      </button>
                    </div>
                  )}
                  <div className="messages">
                    {msgs
                      .filter(visibleChatMsg)
                      .map((m, i) => {
                        const timeLabel = formatMsgTime(m.created_at, tz, locale);
                        const segs =
                          m.role === "assistant" && !m.streaming
                            ? displayParagraphs(m.content || "")
                            : [];
                        const useSegs = segs.length > 1;
                        if (useSegs) {
                          return (
                            <div key={m.id || i} className="bubble-group">
                              {isReminderMsg(m) && <span className="tag-reminder">{t("reminder")}</span>}
                              {segs.map((seg, si) => (
                                <div
                                  key={`${m.id || i}-${si}`}
                                  className={`bubble assistant seg${isReminderMsg(m) ? " reminder" : ""}`}
                                >
                                  {seg}
                                </div>
                              ))}
                              {!!m.attachments?.length && session && (
                                <div className="attach-list">
                                  {m.attachments.map((a) => {
                                    const href = api.workspaceFileUrl(session.id, a.path);
                                    if (a.kind === "image") {
                                      return (
                                        <a
                                          key={a.path}
                                          className="attach-card image"
                                          href={href}
                                          target="_blank"
                                          rel="noreferrer"
                                        >
                                          <img src={href} alt={a.name} />
                                          <span>
                                            {a.name} · {formatBytes(a.bytes)}
                                          </span>
                                        </a>
                                      );
                                    }
                                    return (
                                      <a
                                        key={a.path}
                                        className="attach-card"
                                        href={href}
                                        download={a.name}
                                      >
                                        <strong>{a.name}</strong>
                                        <span>{formatBytes(a.bytes)}</span>
                                      </a>
                                    );
                                  })}
                                </div>
                              )}
                              {timeLabel && <time className="bubble-time">{timeLabel}</time>}
                            </div>
                          );
                        }
                        return (
                        <div
                          key={m.id || i}
                          className={`bubble ${m.role}${isReminderMsg(m) ? " reminder" : ""}`}
                        >
                          {isReminderMsg(m) && <span className="tag-reminder">{t("reminder")}</span>}
                          {m.content || (m.streaming ? "…" : "")}
                          {!!m.attachments?.length && session && (
                            <div className="attach-list">
                              {m.attachments.map((a) => {
                                const href = api.workspaceFileUrl(session.id, a.path);
                                if (a.kind === "image") {
                                  return (
                                    <a
                                      key={a.path}
                                      className="attach-card image"
                                      href={href}
                                      target="_blank"
                                      rel="noreferrer"
                                    >
                                      <img src={href} alt={a.name} />
                                      <span>
                                        {a.name} · {formatBytes(a.bytes)}
                                      </span>
                                    </a>
                                  );
                                }
                                return (
                                  <a key={a.path} className="attach-card" href={href} download={a.name}>
                                    <strong>{a.name}</strong>
                                    <span>{formatBytes(a.bytes)}</span>
                                  </a>
                                );
                              })}
                            </div>
                          )}
                          {timeLabel && <time className="bubble-time">{timeLabel}</time>}
                        </div>
                        );
                      })}
                    <div ref={bottomRef} />
                  </div>
                </>
              )}
              {error && <div className="error">{error}</div>}
              <div className="composer">
                <textarea
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  placeholder={t("composer.placeholder")}
                  rows={2}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      void send();
                    }
                  }}
                />
                <button type="button" disabled={busy || !input.trim()} onClick={() => void send()}>
                  {t("send")}
                </button>
              </div>
            </div>
          </div>
        )}

        {tab === "persona" && (
          <div className="persona-stage">
            <div className="persona-layout">
              <aside className="persona-rail">
                <div className="section-head">
                  <h1>{t("persona.title")}</h1>
                  <p className="sub">{t("persona.subtitle")}</p>
                </div>
                <div className="persona-rail-actions">
                  <button type="button" className="btn ghost" onClick={startNewPersonaDraft}>
                    {t("new")}
                  </button>
                  <label className="btn ghost file-btn">
                    {t("persona.import")}
                    <input
                      type="file"
                      accept=".json,.png,application/json,image/png"
                      hidden
                      onChange={(e) => {
                        const f = e.target.files?.[0];
                        e.target.value = "";
                        if (!f) return;
                        void (async () => {
                          try {
                            const p = await api.importPersonaFile(f);
                            await loadPersonas();
                            loadPersonaIntoDraft(p);
                            setPersonaMsg(`已导入：${p.name}`);
                          } catch (err) {
                            setPersonaMsg(err instanceof Error ? err.message : String(err));
                          }
                        })();
                      }}
                    />
                  </label>
                </div>
                <ul className="persona-list">
                  {personas.map((p) => {
                    const active =
                      personaEditId === p.id ||
                      (!personaEditId && p.id === session?.persona_id);
                    const sourceLabel =
                      p.source === "builtin"
                        ? t("persona.builtin")
                        : p.source === "import"
                          ? t("persona.import")
                          : t("persona.custom");
                    return (
                      <li key={p.id}>
                        <button
                          type="button"
                          className={`persona-card ${active ? "active" : ""}`}
                          onClick={() => loadPersonaIntoDraft(p)}
                        >
                          {p.has_avatar ? (
                            <img
                              className="persona-avatar"
                              src={api.personaAvatarUrl(p.id)}
                              alt=""
                            />
                          ) : (
                            <span className="persona-avatar placeholder">
                              {p.name.slice(0, 1)}
                            </span>
                          )}
                          <span className="persona-card-meta">
                            <strong>{p.name}</strong>
                            <em>
                              {sourceLabel}
                              {p.readonly ? ` · ${t("persona.readonly")}` : ""}
                              {p.id === session?.persona_id ? ` · ${t("persona.current")}` : ""}
                              {p.id === settings?.default_persona_id ? ` · ${t("persona.default")}` : ""}
                            </em>
                          </span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </aside>

              <section className="persona-editor">
                <div className="persona-editor-head">
                  <div>
                    <h2>{personaDraft.name.trim() || (personaEditId ? t("persona.edit") : t("persona.newTitle"))}</h2>
                    <p className="sub">
                      {personaReadonly
                        ? t("persona.readonlyHint")
                        : t("persona.editHint")}
                    </p>
                  </div>
                  <div className="persona-editor-actions">
                    <button
                      type="button"
                      className="btn ghost"
                      disabled={!session || !personaEditId}
                      onClick={() => {
                        if (personaEditId) void switchPersona(personaEditId);
                      }}
                    >
                      {t("persona.useCurrent")}
                    </button>
                    <button
                      type="button"
                      className="btn ghost"
                      disabled={!personaEditId}
                      onClick={() => {
                        if (!personaEditId) return;
                        void api
                          .updateSettings({ default_persona_id: personaEditId })
                          .then((s) => {
                            setSettings(s);
                            setPersonaMsg("已设为默认人格");
                          })
                          .catch((err) => setPersonaMsg(String(err)));
                      }}
                    >
                      {t("persona.makeDefault")}
                    </button>
                  </div>
                </div>

                {personaMsg && <div className="persona-toast">{personaMsg}</div>}

                <div className="form persona-form">
                  <div className="persona-field-group">
                    <h3 className="persona-group-title">{t("persona.basic")}</h3>
                    <div className="persona-identity">
                      <div className="persona-avatar-block">
                        {personaEditId &&
                        personas.find((x) => x.id === personaEditId)?.has_avatar ? (
                          <img
                            className="persona-avatar lg"
                            src={api.personaAvatarUrl(personaEditId)}
                            alt=""
                          />
                        ) : (
                          <span className="persona-avatar lg placeholder">
                            {(personaDraft.name || "?").slice(0, 1)}
                          </span>
                        )}
                        {personaEditId && !personaReadonly && (
                          <label className="btn ghost file-btn compact">
                            {t("persona.changeAvatar")}
                            <input
                              type="file"
                              accept="image/*"
                              hidden
                              onChange={(e) => {
                                const f = e.target.files?.[0];
                                e.target.value = "";
                                if (!f || !personaEditId) return;
                                void api
                                  .uploadPersonaAvatar(personaEditId, f)
                                  .then(async () => {
                                    await loadPersonas();
                                    setPersonaMsg("头像已更新");
                                  })
                                  .catch((err) => setPersonaMsg(String(err)));
                              }}
                            />
                          </label>
                        )}
                      </div>
                      <label className="grow">
                        {t("name")}
                        <input
                          value={personaDraft.name}
                          onChange={(e) =>
                            setPersonaDraft((d) => ({ ...d, name: e.target.value }))
                          }
                          placeholder={t("persona.namePlaceholder")}
                        />
                      </label>
                    </div>
                  </div>

                  <div className="persona-field-group">
                    <h3 className="persona-group-title">{t("persona.profile")}</h3>
                    <label>
                      {t("persona.description")}
                      <span className="field-hint">{t("persona.descriptionHint")}</span>
                      <textarea
                        rows={5}
                        value={personaDraft.description}
                        onChange={(e) =>
                          setPersonaDraft((d) => ({ ...d, description: e.target.value }))
                        }
                        placeholder={t("persona.descriptionPlaceholder")}
                      />
                    </label>
                    <div className="persona-field-row">
                      <label>
                        {t("persona.personality")}
                        <span className="field-hint">personality</span>
                        <textarea
                          rows={4}
                          value={personaDraft.personality}
                          onChange={(e) =>
                            setPersonaDraft((d) => ({ ...d, personality: e.target.value }))
                          }
                          placeholder={t("persona.personalityPlaceholder")}
                        />
                      </label>
                      <label>
                        {t("persona.scenario")}
                        <span className="field-hint">scenario</span>
                        <textarea
                          rows={4}
                          value={personaDraft.scenario}
                          onChange={(e) =>
                            setPersonaDraft((d) => ({ ...d, scenario: e.target.value }))
                          }
                          placeholder={t("persona.scenarioPlaceholder")}
                        />
                      </label>
                    </div>
                  </div>

                  <div className="persona-field-group">
                    <h3 className="persona-group-title">{t("persona.greetings")}</h3>
                    <label>
                      {t("persona.opener")}
                      <span className="field-hint">{t("persona.openerHint")}</span>
                      <textarea
                        rows={3}
                        value={personaDraft.opener}
                        onChange={(e) =>
                          setPersonaDraft((d) => ({ ...d, opener: e.target.value }))
                        }
                        placeholder={t("persona.openerPlaceholder")}
                      />
                    </label>
                    <label>
                      {t("persona.alternates")}
                      <span className="field-hint">{t("persona.alternatesHint")}</span>
                      <textarea
                        rows={4}
                        value={personaDraft.alternate_greetings}
                        onChange={(e) =>
                          setPersonaDraft((d) => ({
                            ...d,
                            alternate_greetings: e.target.value,
                          }))
                        }
                        placeholder={"另一句开场\n---\n再一句开场"}
                      />
                    </label>
                  </div>

                  <div className="persona-field-group">
                    <h3 className="persona-group-title">{t("persona.advanced")}</h3>
                    <label>
                      {t("persona.systemPrompt")}
                      <span className="field-hint">{t("persona.systemPromptHint")}</span>
                      <textarea
                        rows={4}
                        value={personaDraft.system_prompt}
                        onChange={(e) =>
                          setPersonaDraft((d) => ({ ...d, system_prompt: e.target.value }))
                        }
                        placeholder={t("persona.systemPromptPlaceholder")}
                      />
                    </label>
                    <label>
                      {t("persona.postHistory")}
                      <span className="field-hint">{t("persona.postHistoryHint")}</span>
                      <textarea
                        rows={2}
                        value={personaDraft.post_history_instructions}
                        onChange={(e) =>
                          setPersonaDraft((d) => ({
                            ...d,
                            post_history_instructions: e.target.value,
                          }))
                        }
                        placeholder={t("persona.postHistoryPlaceholder")}
                      />
                    </label>
                    {loreEntries.length > 0 && (
                      <div className="lore-preview">
                        <p className="persona-group-title">{t("persona.lorebook")}</p>
                        <p className="sub">{t("persona.loreCount", { count: loreEntries.length })}</p>
                        <ul>
                          {loreEntries.slice(0, 20).map((entry, i) => {
                            const keys = Array.isArray(entry.keys)
                              ? (entry.keys as string[]).join("、")
                              : "";
                            const content = String(entry.content || "");
                            const constant = !!entry.constant;
                            return (
                              <li key={i}>
                                <strong>{constant ? t("persona.always") : keys || t("persona.noKeywords")}</strong>
                                <span>
                                  {content.slice(0, 120)}
                                  {content.length > 120 ? "…" : ""}
                                </span>
                              </li>
                            );
                          })}
                        </ul>
                      </div>
                    )}
                  </div>

                  <div className="persona-form-footer">
                    <button type="button" onClick={() => void savePersonaDraft()}>
                      {personaReadonly ? t("persona.saveAs") : t("save")}
                    </button>
                    {personaEditId && !personaReadonly && (
                      <button
                        type="button"
                        className="btn ghost danger"
                        onClick={() => {
                          if (!personaEditId) return;
                          if (!window.confirm("删除该人格？")) return;
                          void api
                            .deletePersona(personaEditId)
                            .then(async () => {
                              startNewPersonaDraft();
                              await loadPersonas();
                              setPersonaMsg("已删除");
                            })
                            .catch((err) => setPersonaMsg(String(err)));
                        }}
                      >
                        {t("delete")}
                      </button>
                    )}
                  </div>
                </div>
              </section>
            </div>
          </div>
        )}

        {tab === "memory" && (
          <div className="memory-stage">
            <div className="memory-column">
              <div className="section-head">
                <h1>{t("memory.title")}</h1>
                <p className="sub">{summary?.headline || t("loading")}</p>
              </div>
              <div className="row">
                <button type="button" className="btn ghost" onClick={() => void refreshMemory()}>
                  {t("refresh")}
                </button>
                <button
                  type="button"
                  className="btn ghost"
                  onClick={() => void api.consolidate().then(refreshMemory)}
                >
                  {t("consolidate")}
                </button>
              </div>
              <div className="form remember-form">
                <label>
                  {t("memory.correctLabel")}
                  <input
                    value={correction}
                    onChange={(e) => setCorrection(e.target.value)}
                    placeholder={t("memory.correctPlaceholder")}
                  />
                </label>
                <button
                  type="button"
                  className="btn"
                  disabled={!correction.trim()}
                  onClick={() =>
                    void api.postCorrection(correction.trim()).then(() => {
                      setCorrection("");
                      return refreshMemory();
                    })
                  }
                >
                  {t("memory.write")}
                </button>
              </div>
              <div className="summary-sections">
                {(summary?.sections || []).map((sec) => (
                  <div key={sec.kind} className="summary-section">
                    <div className="kind-label">
                      {sec.label}
                      <button
                        type="button"
                        className="btn danger compact"
                        onClick={() => {
                          if (!confirm(`确认归档「${sec.label}」下的记忆？`)) return;
                          void api.archiveKind(sec.kind).then(refreshMemory);
                        }}
                      >
                        {t("archive")}
                      </button>
                    </div>
                    {sec.items.map((it) => (
                      <button
                        type="button"
                        className={`atom-btn ${atomDetail?.key === it.key ? "on" : ""}`}
                        key={it.key}
                        onClick={() => void openAtom(it.key)}
                      >
                        <div>{it.statement}</div>
                      </button>
                    ))}
                  </div>
                ))}
                {summary && summary.total === 0 && (
                  <p className="sub">{t("memory.empty")}</p>
                )}
              </div>
              {atomDetail && (
                <div className="memory-detail">
                  <h2 className="atom-title">{atomDetail.statement}</h2>
                  <div className="row">
                    <span className="meta-chip">{atomDetail.kind}</span>
                    <span className="meta-chip">{atomDetail.key}</span>
                    <button
                      type="button"
                      className="btn danger"
                      onClick={() =>
                        void api.archiveAtom(atomDetail.key).then(() => {
                          setAtomDetail(null);
                          return refreshMemory();
                        })
                      }
                    >
                      {t("archive")}
                    </button>
                  </div>
                    <pre className="code">{atomDetail.detail || "—"}</pre>
                </div>
              )}
              {error && <div className="error">{error}</div>}
            </div>
          </div>
        )}

        {tab === "settings" && settings && (
          <div className="settings-stage">
            <div className="settings-column">
              <div className="section-head">
                <h1>{t("settings.title")}</h1>
                <p className="sub">
                  预设 {settings.preset_id} · sidecar {settings.start_memory_sidecar ? "on" : "off"}
                  {settings.reminders_enabled ? " · 提醒 on" : " · 提醒 off"}
                  {" · "}Skills {skillsInfo ? skillsInfo.count : "…"}
                  {skillsInfo?.skills_dir ? ` · ${skillsInfo.skills_dir}` : ""}
                </p>
                <p className="sub">
                  MCP{" "}
                  {mcpStatus
                    ? `${mcpStatus.enabled ? "on" : "off"} · 服 ${mcpStatus.servers.filter((s) => s.ok).length}/${mcpStatus.servers.length} · 工具 ${mcpStatus.tool_count}`
                    : "…"}
                  {mcpStatus?.config_path ? ` · ${mcpStatus.config_path}` : ""}
                </p>
              </div>
              <form
                className="form"
                onSubmit={(e) => void saveSettings(e)}
                key={settings.user_timezone + settings.quiet_hours_start}
              >
                <h2 className="settings-section">{t("settings.model")}</h2>
                <label className="chk-inline">
                  <input
                    type="checkbox"
                    checked={Boolean(
                      localLlm?.enabled ||
                        localLlmBusy ||
                        localLlm?.download?.status === "downloading" ||
                        localLlm?.download?.status === "extracting",
                    )}
                    disabled={
                      localLlmBusy &&
                      localLlm?.download?.status !== "downloading" &&
                      localLlm?.download?.status !== "extracting" &&
                      !localLlm?.enabled
                    }
                    onChange={(e) => void toggleLocalLlm(e.target.checked)}
                  />
                  {t("settings.localModel")}
                </label>
                <p className="sub">
                  {(() => {
                    const dl = localLlm?.download;
                    if (dl?.status === "error") {
                      return `错误：${dl.error || localLlmMsg || "unknown"}`;
                    }
                    if (dl?.status === "cancelled") return "已取消下载";
                    if (dl?.status === "downloading") {
                      const pct = dl.percent != null ? `${dl.percent.toFixed(1)}%` : "…";
                      const mb = (dl.bytes_done / (1024 * 1024)).toFixed(0);
                      const total =
                        dl.bytes_total > 0
                          ? `${(dl.bytes_total / (1024 * 1024)).toFixed(0)} MB`
                          : "~2.8 GB";
                      return `下载中 ${pct}（${mb} / ${total}）${dl.label ? ` · ${dl.label}` : ""}`;
                    }
                    if (dl?.status === "extracting") return "正在解压 llama-server…";
                    if (localLlm?.enabled && localLlm.running) {
                      return `本地就绪 · ${localLlm.model || "Qwen3.8-4B Distill"}`;
                    }
                    if (localLlm?.enabled && localLlm.ready) {
                      return `已下载，正在启动 llama-server…`;
                    }
                    if (localLlmBusy) return localLlmMsg || "正在准备本地模型…";
                    if (localLlmMsg) return localLlmMsg;
                    return "开启后自动下载约 2.8GB 并切换到本机推理；关闭则恢复云端配置。";
                  })()}
                </p>
                {(localLlm?.download?.status === "downloading" ||
                  localLlm?.download?.status === "extracting") && (
                  <button
                    type="button"
                    className="btn ghost"
                    onClick={() =>
                      void api.cancelLocalLlmDownload().then((st) => {
                        setLocalLlm(st);
                        setLocalLlmBusy(Boolean(st.busy));
                        setLocalLlmMsg("已请求取消下载");
                      })
                    }
                  >
                    {t("settings.cancelDownload")}
                  </button>
                )}
                <label>
                  LLM Base URL
                  <input name="llm_base_url" defaultValue={settings.llm_base_url} />
                </label>
                <label>
                  Model
                  <input name="llm_model" defaultValue={settings.llm_model} />
                </label>
                <label>
                  API Key {settings.llm_api_key_set ? t("settings.apiKeyConfigured") : ""}
                  <input name="llm_api_key" type="password" placeholder="sk-… / EMPTY" autoComplete="off" />
                </label>
                <label className="chk-inline">
                  <input name="tts_enabled" type="checkbox" defaultChecked={settings.tts_enabled} />
                  {t("settings.tts")}
                </label>
                <label className="chk-inline">
                  <input
                    name="show_memory_hints"
                    type="checkbox"
                    defaultChecked={settings.show_memory_hints}
                  />
                  {t("settings.memoryHints")}
                </label>
                <label>
                  atom-memory Base URL
                  <input name="atom_memory_base_url" defaultValue={settings.atom_memory_base_url} />
                </label>
                <label>
                  {t("settings.defaultWarmth")}
                  <input
                    name="default_warmth"
                    type="number"
                    min={0}
                    max={100}
                    defaultValue={settings.default_warmth}
                  />
                </label>
                <h2 className="settings-section">{t("settings.tools")}</h2>
                <label className="chk-inline">
                  <input
                    name="computer_enabled"
                    type="checkbox"
                    defaultChecked={settings.computer_enabled === true}
                  />
                  {t("settings.localTools")}
                </label>
                <p className="sub">
                  {t("settings.localToolsHint")}
                </p>
                <label className="chk-inline">
                  <input
                    name="web_search_enabled"
                    type="checkbox"
                    defaultChecked={settings.web_search_enabled !== false}
                  />
                  {t("settings.webSearch")}
                </label>
                <label>
                  Tavily API Key {settings.tavily_api_key_set ? t("settings.apiKeyConfigured") : ""}
                  <input name="tavily_api_key" type="password" placeholder="tvly-…" autoComplete="off" />
                </label>
                <h2 className="settings-section">{t("settings.time")}</h2>
                <label>
                  {t("settings.timezone")}
                  <input name="user_timezone" defaultValue={settings.user_timezone} />
                </label>
                {tzSuggest && tzSuggest !== settings.user_timezone && (
                  <p className="sub">
                    {t("settings.browserSuggest", { tz: tzSuggest })}{" "}
                    <button
                      type="button"
                      className="btn ghost"
                      onClick={(ev) => {
                        const form = (ev.target as HTMLElement).closest("form");
                        const inputEl = form?.querySelector<HTMLInputElement>(
                          'input[name="user_timezone"]',
                        );
                        if (inputEl) inputEl.value = tzSuggest;
                      }}
                    >
                      {t("settings.fill")}
                    </button>
                  </p>
                )}
                <label className="chk-inline">
                  <input
                    name="quiet_hours_enabled"
                    type="checkbox"
                    defaultChecked={settings.quiet_hours_enabled}
                  />
                  {t("settings.quietHours")}
                </label>
                <label>
                  {t("settings.quietStart")}
                  <input
                    name="quiet_hours_start"
                    defaultValue={settings.quiet_hours_start}
                    placeholder="22:00"
                  />
                </label>
                <label>
                  {t("settings.quietEnd")}
                  <input
                    name="quiet_hours_end"
                    defaultValue={settings.quiet_hours_end}
                    placeholder="08:00"
                  />
                </label>
                <button type="submit" className="btn">
                  {t("save")}
                </button>
              </form>

              <h2 className="settings-section">{t("settings.mcp")}</h2>
              <p className="sub">{t("settings.mcpHint")}</p>
              {mcpMsg && <p className="sub">{mcpMsg}</p>}
              <div className="mcp-list">
                {mcpRows.map((row, idx) => {
                  const isHttp = Boolean(row.entry.url);
                  const argsStr = Array.isArray(row.entry.args)
                    ? row.entry.args.join(" ")
                    : String(row.entry.args || "");
                  return (
                    <div className="mcp-card" key={`${row.name}-${idx}`}>
                      <div className="row">
                        <label className="chk-inline">
                          <input
                            type="checkbox"
                            checked={row.entry.active !== false}
                            onChange={(e) =>
                              updateMcpRow(idx, { entry: { active: e.target.checked } })
                            }
                          />
                          active
                        </label>
                        <input
                          type="text"
                          placeholder={t("settings.serverName")}
                          value={row.name}
                          onChange={(e) => updateMcpRow(idx, { name: e.target.value })}
                        />
                        <select
                          value={isHttp ? "http" : "stdio"}
                          onChange={(e) => {
                            if (e.target.value === "http") {
                              updateMcpRow(idx, {
                                entry: {
                                  url: row.entry.url || "http://127.0.0.1:8000/mcp",
                                  transport: "streamable_http",
                                  command: undefined,
                                  args: undefined,
                                },
                              });
                            } else {
                              updateMcpRow(idx, {
                                entry: {
                                  command: row.entry.command || "npx",
                                  args: row.entry.args || ["-y", "demo"],
                                  url: undefined,
                                  transport: undefined,
                                },
                              });
                            }
                          }}
                        >
                          <option value="stdio">stdio</option>
                          <option value="http">HTTP/SSE</option>
                        </select>
                      </div>
                      {isHttp ? (
                        <div className="row">
                          <input
                            type="text"
                            placeholder="URL"
                            value={String(row.entry.url || "")}
                            onChange={(e) => updateMcpRow(idx, { entry: { url: e.target.value } })}
                          />
                          <input
                            type="text"
                            placeholder="transport"
                            value={String(row.entry.transport || row.entry.type || "streamable_http")}
                            onChange={(e) =>
                              updateMcpRow(idx, { entry: { transport: e.target.value } })
                            }
                          />
                        </div>
                      ) : (
                        <div className="row">
                          <input
                            type="text"
                            placeholder="command"
                            value={String(row.entry.command || "")}
                            onChange={(e) =>
                              updateMcpRow(idx, { entry: { command: e.target.value } })
                            }
                          />
                          <input
                            type="text"
                            placeholder={t("settings.args")}
                            value={argsStr}
                            onChange={(e) =>
                              updateMcpRow(idx, {
                                entry: {
                                  args: e.target.value.split(/\s+/).filter(Boolean),
                                },
                              })
                            }
                          />
                        </div>
                      )}
                      <div className="row">
                        <button type="button" className="btn ghost" onClick={() => void testMcpRow(idx)}>
                          {t("settings.test")}
                        </button>
                        <button
                          type="button"
                          className="btn danger"
                          onClick={() => setMcpRows((xs) => xs.filter((_, i) => i !== idx))}
                        >
                          {t("delete")}
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
              <div className="row" style={{ gap: "0.5rem", flexWrap: "wrap" }}>
                <button
                  type="button"
                  className="btn ghost"
                  onClick={() =>
                    setMcpRows((xs) => [
                      ...xs,
                      {
                        name: `server_${xs.length + 1}`,
                        entry: { active: false, command: "npx", args: ["-y", "demo"] },
                      },
                    ])
                  }
                >
                  {t("settings.addServer")}
                </button>
                <button type="button" className="btn" onClick={() => void saveMcpAndReload()}>
                  {t("settings.saveReload")}
                </button>
                <button
                  type="button"
                  className="btn ghost"
                  onClick={() =>
                    void api.reloadMcp().then((st) => {
                      setMcpStatus(st);
                      setMcpMsg(`已重载 · 工具 ${st.tool_count}`);
                    })
                  }
                >
                  {t("settings.reload")}
                </button>
              </div>

              <h2 className="settings-section">{t("settings.advanced")}</h2>
              <label className="chk-inline">
                <input type="checkbox" checked={debugOn} onChange={(e) => toggleDebug(e.target.checked)} />
                {t("settings.debug")}
              </label>
              {debugOn && (
                <div className="debug-panel">
                  <div className="row">
                    <span className={`meta-chip ${status?.llm.ok ? "ok" : "bad"}`}>
                      llm {status?.llm.ok ? "ok" : "?"}
                    </span>
                    <span className={`meta-chip ${status?.memory.ok ? "ok" : "bad"}`}>
                      memory {status?.memory.ok ? "ok" : "?"}
                    </span>
                    <button type="button" className="btn ghost" onClick={() => void probe()}>
                      {t("settings.probe")}
                    </button>
                    <button
                      type="button"
                      className="btn ghost"
                      onClick={() => void api.consolidate().then((r) => pushLog("consolidate", r, false))}
                    >
                      {t("settings.manualConsolidate")}
                    </button>
                    <a
                      href={`${memBase.replace(/\/$/, "")}/ui?uid=${encodeURIComponent(space)}`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      atom /ui
                    </a>
                    <button type="button" className="btn ghost" onClick={() => setLogs([])}>
                      {t("settings.clearLogs")}
                    </button>
                  </div>
                  <div className="log-list">
                    {logs.length === 0 && <p className="sub">{t("settings.noLogs")}</p>}
                    {logs.map((l) => (
                      <details key={l.id} className="log" open={l.open}>
                        <summary>
                          [{l.t}] {l.title}
                        </summary>
                        <pre>
                          {typeof l.payload === "string" ? l.payload : JSON.stringify(l.payload, null, 2)}
                        </pre>
                      </details>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        )}
      </main>

      {scheduleOpen && (
        <>
          <button
            type="button"
            className="drawer-backdrop"
            aria-label={t("schedule.closeAria")}
            onClick={() => setScheduleOpen(false)}
          />
          <aside className="drawer">
            <div className="drawer-head">
              <h1>{t("schedule.title")}</h1>
              <button type="button" className="btn ghost" onClick={() => setScheduleOpen(false)}>
                {t("close")}
              </button>
            </div>
            <p className="sub">
              {t("schedule.subtitle", { tz })}
            </p>
            <div className="form rem-form">
              <label>
                {t("schedule.note")}
                <input value={remNote} onChange={(e) => setRemNote(e.target.value)} placeholder={t("schedule.notePlaceholder")} />
              </label>
              <label>
                {t("schedule.repeat")}
                <select
                  value={remRepeat}
                  onChange={(e) =>
                    setRemRepeat(
                      e.target.value as "once" | "daily" | "weekdays" | "weekly" | "custom",
                    )
                  }
                >
                  <option value="once">{t("schedule.once")}</option>
                  <option value="daily">{t("schedule.daily")}</option>
                  <option value="weekdays">{t("schedule.weekdays")}</option>
                  <option value="weekly">{t("schedule.weekly")}</option>
                  <option value="custom">{t("schedule.custom")}</option>
                </select>
              </label>
              {remRepeat !== "custom" && (
                <label>
                  {remRepeat === "once" ? t("schedule.time") : t("schedule.firstTime")}
                  <input
                    type="datetime-local"
                    value={remDueLocal}
                    onChange={(e) => setRemDueLocal(e.target.value)}
                  />
                </label>
              )}
              {remRepeat === "custom" && (
                <label>
                  Cron（五段）
                  <input
                    value={remCron}
                    onChange={(e) => setRemCron(e.target.value)}
                    placeholder="0 9 * * 1-5"
                  />
                </label>
              )}
              <button
                type="button"
                className="btn"
                disabled={
                  !remNote.trim() || (remRepeat === "custom" ? !remCron.trim() : !remDueLocal)
                }
                onClick={() => void createReminder()}
              >
                {t("schedule.create")}
              </button>
            </div>
            <ul className="rem-list">
              {reminders.length === 0 && <li className="sub">{t("schedule.empty")}</li>}
              {reminders.map((r) => (
                <li key={r.id}>
                  <div>
                    <strong>{r.note}</strong>
                    <div className="meta">
                      {t("schedule.next", { time: formatInTz(r.due_at, tz) })}
                      {r.schedule_kind === "cron" && r.cron_expr
                        ? t("schedule.recurring", { cron: r.cron_expr })
                        : t("schedule.oneOff")}
                    </div>
                  </div>
                  <button
                    type="button"
                    className="btn ghost"
                    onClick={() =>
                      void api.cancelReminder(r.id).then(() => session && loadReminders(session.id))
                    }
                  >
                    {t("cancel")}
                  </button>
                </li>
              ))}
            </ul>
          </aside>
        </>
      )}
    </div>
  );
}
