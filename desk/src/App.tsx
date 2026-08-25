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
  type Reminder,
  type Session,
  type Settings,
  type SkillsList,
  type Turn,
} from "./api";

type McpRow = { name: string; entry: McpServerEntry };

type Tab = "chat" | "memory" | "settings";
type Msg = {
  id?: string;
  role: "user" | "assistant";
  content: string;
  streaming?: boolean;
  source?: string;
  attachments?: Array<{ path: string; name: string; bytes: number; kind: string }>;
};
type LogItem = { id: number; t: string; title: string; payload: unknown; open?: boolean };

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

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
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
    attachments: t.attachments || [],
  };
}

function sessionLabel(s: Session, tz: string): string {
  const stamp = s.updated_at || s.created_at;
  try {
    return new Date(stamp).toLocaleString("zh-CN", {
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
  const bottomRef = useRef<HTMLDivElement>(null);
  const warmthTimer = useRef<number | null>(null);
  const lastTurnCount = useRef(0);

  const tz = settings?.user_timezone || "Asia/Shanghai";

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

  async function selectSession(s: Session, opener?: string | null) {
    setSession(s);
    setWarmth(s.warmth ?? 35);
    setMemHint(null);
    const turns = await api.listTurns(s.id);
    lastTurnCount.current = turns.length;
    const welcome = opener ?? settings?.persona_opener;
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
        let list = await api.listSessions();
        if (!list.length) {
          const created = await api.createSession({ warmth: s.default_warmth });
          list = [created];
        }
        setSessions(list);
        await selectSession(list[0], s.persona_opener);
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
    pushLog("user_message", { text }, false);
    setMsgs((m) => [
      ...m.filter((x) => x.source !== "opener"),
      { role: "user", content: text },
      { role: "assistant", content: "", streaming: true },
    ]);
    let acc = "";
    const hints: string[] = [];
    try {
      await streamChat(session.id, text, {
        onToken: (t) => {
          acc += t;
          setMsgs((m) => {
            const copy = [...m];
            copy[copy.length - 1] = { role: "assistant", content: acc, streaming: true };
            return copy;
          });
        },
        onEvent: (type, data) => {
          if (type === "token") return;
          pushLog(type, data, type === "error");
          if (type === "memory_recall" && data && typeof data === "object" && "has_block" in data) {
            if ((data as { has_block?: boolean }).has_block) hints.push("本轮用到了长期记忆");
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
        copy[copy.length - 1] = { role: "assistant", content: acc || copy[copy.length - 1].content };
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
    const s = await api.createSession({ warmth });
    setSessions((xs) => [s, ...xs]);
    await selectSession(s);
    pushLog("new_session", s, false);
    await probe();
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

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar-brand">
          <span className="brand-name">Velora</span>
          <span className="brand-tag">私人秘书</span>
        </div>
        <nav className="topbar-nav">
          <button type="button" className={tab === "chat" ? "active" : ""} onClick={() => setTab("chat")}>
            对话
          </button>
          <button type="button" className={tab === "memory" ? "active" : ""} onClick={() => setTab("memory")}>
            记忆
          </button>
          <button
            type="button"
            className={tab === "settings" ? "active" : ""}
            onClick={() => setTab("settings")}
          >
            设置
          </button>
        </nav>
        <div className="topbar-actions">
          {tab === "chat" && (
            <>
              <button
                type="button"
                className={`btn ghost ${scheduleOpen ? "active" : ""}`}
                onClick={() => setScheduleOpen(true)}
              >
                日程
              </button>
              <div className="session-menu">
                <select
                  value={session?.id || ""}
                  onChange={(e) => {
                    const s = sessions.find((x) => x.id === e.target.value);
                    if (s) void selectSession(s);
                  }}
                  aria-label="会话"
                >
                  {sessions.map((s) => (
                    <option key={s.id} value={s.id}>
                      {sessionLabel(s, tz)}
                    </option>
                  ))}
                </select>
                <button type="button" className="btn ghost" onClick={() => void newSession()}>
                  新会话
                </button>
              </div>
              <button
                type="button"
                className={`btn ghost ${prefsOpen ? "active" : ""}`}
                onClick={() => setPrefsOpen((v) => !v)}
              >
                语气
              </button>
            </>
          )}
        </div>
      </header>

      {prefsOpen && tab === "chat" && (
        <div className="prefs-bar">
          <label className="warmth-pick">
            <span>更助理</span>
            <input
              type="range"
              min={0}
              max={100}
              value={warmth}
              onChange={(e) => onWarmthChange(Number(e.target.value))}
            />
            <span>更陪伴 ({warmth})</span>
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
                  <h1>{settings?.persona_name || "日常助理"}</h1>
                  <p className="sub">同一会话接上上下文；值得留下的会记入长期记忆。</p>
                </div>
              ) : (
                <>
                  <div className="chat-head">
                    <h1>{settings?.persona_name || "日常助理"}</h1>
                  </div>
                  {memHint && (
                    <div className="mem-hint">
                      <span>{memHint}</span>
                      <button type="button" className="btn ghost" onClick={() => setMemHint(null)}>
                        关闭
                      </button>
                    </div>
                  )}
                  <div className="messages">
                    {msgs
                      .filter(visibleChatMsg)
                      .map((m, i) => (
                        <div
                          key={m.id || i}
                          className={`bubble ${m.role}${isReminderMsg(m) ? " reminder" : ""}`}
                        >
                          {isReminderMsg(m) && <span className="tag-reminder">提醒</span>}
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
                        </div>
                      ))}
                    <div ref={bottomRef} />
                  </div>
                </>
              )}
              {error && <div className="error">{error}</div>}
              <div className="composer">
                <textarea
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  placeholder="说点什么…（Enter 发送；「请提醒：事项 | ISO」可设提醒）"
                  rows={2}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      void send();
                    }
                  }}
                />
                <button type="button" disabled={busy || !input.trim()} onClick={() => void send()}>
                  发送
                </button>
              </div>
            </div>
          </div>
        )}

        {tab === "memory" && (
          <div className="memory-stage">
            <div className="memory-column">
              <div className="section-head">
                <h1>我记得什么</h1>
                <p className="sub">{summary?.headline || "加载中…"}</p>
              </div>
              <div className="row">
                <button type="button" className="btn ghost" onClick={() => void refreshMemory()}>
                  刷新
                </button>
                <button
                  type="button"
                  className="btn ghost"
                  onClick={() => void api.consolidate().then(refreshMemory)}
                >
                  立即固化
                </button>
              </div>
              <div className="form remember-form">
                <label>
                  纠正 / 请记住
                  <input
                    value={correction}
                    onChange={(e) => setCorrection(e.target.value)}
                    placeholder="例如：请记住我喜欢被叫小周"
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
                  写入并固化
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
                        归档此类
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
                  <p className="sub">还没有记住什么。可以说「请记住…」。</p>
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
                      归档
                    </button>
                  </div>
                  <pre className="code">{atomDetail.detail || "（空）"}</pre>
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
                <h1>设置</h1>
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
                <h2 className="settings-section">模型</h2>
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
                  使用本地小模型（Qwen3.8-4B Distill）
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
                    取消下载
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
                  API Key {settings.llm_api_key_set ? "（已配置，留空不改）" : ""}
                  <input name="llm_api_key" type="password" placeholder="sk-… / EMPTY" autoComplete="off" />
                </label>
                <label className="chk-inline">
                  <input name="tts_enabled" type="checkbox" defaultChecked={settings.tts_enabled} />
                  TTS（未接通）
                </label>
                <label className="chk-inline">
                  <input
                    name="show_memory_hints"
                    type="checkbox"
                    defaultChecked={settings.show_memory_hints}
                  />
                  对话中显示记忆提示
                </label>
                <label>
                  atom-memory Base URL
                  <input name="atom_memory_base_url" defaultValue={settings.atom_memory_base_url} />
                </label>
                <label>
                  新会话默认 warmth
                  <input
                    name="default_warmth"
                    type="number"
                    min={0}
                    max={100}
                    defaultValue={settings.default_warmth}
                  />
                </label>
                <h2 className="settings-section">本机工具与搜索</h2>
                <label className="chk-inline">
                  <input
                    name="computer_enabled"
                    type="checkbox"
                    defaultChecked={settings.computer_enabled !== false}
                  />
                  启用本机工具（Shell / Python / 文件）
                </label>
                <label className="chk-inline">
                  <input
                    name="web_search_enabled"
                    type="checkbox"
                    defaultChecked={settings.web_search_enabled !== false}
                  />
                  启用网页搜索（Tavily）
                </label>
                <label>
                  Tavily API Key {settings.tavily_api_key_set ? "（已配置，留空不改）" : ""}
                  <input name="tavily_api_key" type="password" placeholder="tvly-…" autoComplete="off" />
                </label>
                <h2 className="settings-section">时区与静默</h2>
                <label>
                  时区（IANA）
                  <input name="user_timezone" defaultValue={settings.user_timezone} />
                </label>
                {tzSuggest && tzSuggest !== settings.user_timezone && (
                  <p className="sub">
                    浏览器建议：{tzSuggest}{" "}
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
                      填入
                    </button>
                  </p>
                )}
                <label className="chk-inline">
                  <input
                    name="quiet_hours_enabled"
                    type="checkbox"
                    defaultChecked={settings.quiet_hours_enabled}
                  />
                  启用静默时段（期间提醒延后）
                </label>
                <label>
                  静默开始（本地 HH:MM）
                  <input
                    name="quiet_hours_start"
                    defaultValue={settings.quiet_hours_start}
                    placeholder="22:00"
                  />
                </label>
                <label>
                  静默结束（本地 HH:MM）
                  <input
                    name="quiet_hours_end"
                    defaultValue={settings.quiet_hours_end}
                    placeholder="08:00"
                  />
                </label>
                <button type="submit" className="btn">
                  保存
                </button>
              </form>

              <h2 className="settings-section">MCP 服务</h2>
              <p className="sub">增改删后点「保存并重载」写入 mcp_server.json 并热重载（不必重启进程）。</p>
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
                          placeholder="名称"
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
                            placeholder="args（空格分隔）"
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
                          测连
                        </button>
                        <button
                          type="button"
                          className="btn danger"
                          onClick={() => setMcpRows((xs) => xs.filter((_, i) => i !== idx))}
                        >
                          删除
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
                  添加服务
                </button>
                <button type="button" className="btn" onClick={() => void saveMcpAndReload()}>
                  保存并重载
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
                  仅重载
                </button>
              </div>

              <h2 className="settings-section">高级</h2>
              <label className="chk-inline">
                <input type="checkbox" checked={debugOn} onChange={(e) => toggleDebug(e.target.checked)} />
                调试模式
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
                      探测状态
                    </button>
                    <button
                      type="button"
                      className="btn ghost"
                      onClick={() => void api.consolidate().then((r) => pushLog("consolidate", r, false))}
                    >
                      手动固化
                    </button>
                    <a
                      href={`${memBase.replace(/\/$/, "")}/ui?uid=${encodeURIComponent(space)}`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      atom /ui
                    </a>
                    <button type="button" className="btn ghost" onClick={() => setLogs([])}>
                      清空日志
                    </button>
                  </div>
                  <div className="log-list">
                    {logs.length === 0 && <p className="sub">发送消息后出现事件。</p>}
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
            aria-label="关闭日程"
            onClick={() => setScheduleOpen(false)}
          />
          <aside className="drawer">
            <div className="drawer-head">
              <h1>日程</h1>
              <button type="button" className="btn ghost" onClick={() => setScheduleOpen(false)}>
                关闭
              </button>
            </div>
            <p className="sub">
              按时区 {tz} 解释时间；静默时段内会延后投递。到期走完整 Agent（可写文件到会话）。
            </p>
            <div className="form rem-form">
              <label>
                事项
                <input value={remNote} onChange={(e) => setRemNote(e.target.value)} placeholder="明天下午开会" />
              </label>
              <label>
                重复
                <select
                  value={remRepeat}
                  onChange={(e) =>
                    setRemRepeat(
                      e.target.value as "once" | "daily" | "weekdays" | "weekly" | "custom",
                    )
                  }
                >
                  <option value="once">不重复</option>
                  <option value="daily">每天</option>
                  <option value="weekdays">工作日</option>
                  <option value="weekly">每周</option>
                  <option value="custom">自定义 cron</option>
                </select>
              </label>
              {remRepeat !== "custom" && (
                <label>
                  {remRepeat === "once" ? "时间（本地）" : "首次参考时间（本地）"}
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
                创建提醒
              </button>
            </div>
            <ul className="rem-list">
              {reminders.length === 0 && <li className="sub">暂无待办提醒</li>}
              {reminders.map((r) => (
                <li key={r.id}>
                  <div>
                    <strong>{r.note}</strong>
                    <div className="meta">
                      下次 {formatInTz(r.due_at, tz)}
                      {r.schedule_kind === "cron" && r.cron_expr
                        ? ` · 周期 ${r.cron_expr}`
                        : " · 一次"}
                    </div>
                  </div>
                  <button
                    type="button"
                    className="btn ghost"
                    onClick={() =>
                      void api.cancelReminder(r.id).then(() => session && loadReminders(session.id))
                    }
                  >
                    取消
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
