import { useEffect, useRef, useState } from "react";
import {
  api,
  streamChat,
  type AtomRow,
  type DebugStatus,
  type Session,
  type Settings,
  type Turn,
} from "./api";

type Tab = "chat" | "memory" | "settings";
type Msg = { role: "user" | "assistant"; content: string; streaming?: boolean };
type LogItem = { id: number; t: string; title: string; payload: unknown; open?: boolean };

let logSeq = 0;

export default function App() {
  const [tab, setTab] = useState<Tab>("chat");
  const [sessions, setSessions] = useState<Session[]>([]);
  const [session, setSession] = useState<Session | null>(null);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [atoms, setAtoms] = useState<AtomRow[]>([]);
  const [atomDetail, setAtomDetail] = useState<AtomRow | null>(null);
  const [correction, setCorrection] = useState("");
  const [logs, setLogs] = useState<LogItem[]>([]);
  const [status, setStatus] = useState<DebugStatus | null>(null);
  const [warmth, setWarmth] = useState(35);
  const bottomRef = useRef<HTMLDivElement>(null);
  const warmthTimer = useRef<number | null>(null);

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

  async function probe() {
    try {
      setStatus(await api.debugStatus());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function selectSession(s: Session) {
    setSession(s);
    setWarmth(s.warmth ?? 35);
    const turns = await api.listTurns(s.id);
    setMsgs(turns.map((t: Turn) => ({ role: t.role as "user" | "assistant", content: t.content })));
  }

  useEffect(() => {
    void (async () => {
      try {
        const s = await api.getSettings();
        setSettings(s);
        let list = await api.listSessions();
        if (!list.length) {
          const created = await api.createSession({ warmth: s.default_warmth });
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

  async function refreshAtoms() {
    try {
      const data = await api.listAtoms();
      setAtoms(data.results || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    if (tab === "memory") void refreshAtoms();
    if (tab === "settings") void probe();
  }, [tab]);

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
    pushLog("user_message", { text }, false);
    setMsgs((m) => [...m, { role: "user", content: text }, { role: "assistant", content: "", streaming: true }]);
    let acc = "";
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
        },
        onError: (msg) => setError(msg),
      });
      setMsgs((m) => {
        const copy = [...m];
        copy[copy.length - 1] = { role: "assistant", content: acc || copy[copy.length - 1].content };
        return copy;
      });
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

  async function saveSettings(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    const body: {
      llm_base_url: string;
      llm_model: string;
      llm_api_key?: string;
      tts_enabled: boolean;
      atom_memory_base_url: string;
      default_warmth: number;
    } = {
      llm_base_url: String(fd.get("llm_base_url") || ""),
      llm_model: String(fd.get("llm_model") || ""),
      tts_enabled: fd.get("tts_enabled") === "on",
      atom_memory_base_url: String(fd.get("atom_memory_base_url") || ""),
      default_warmth: Number(fd.get("default_warmth") || 35),
    };
    const key = String(fd.get("llm_api_key") || "");
    if (key) body.llm_api_key = key;
    const next = await api.updateSettings(body);
    setSettings(next);
    pushLog("settings_saved", { ...body, llm_api_key: key ? "(set)" : undefined });
    await probe();
  }

  const atomsByKind = atoms.reduce<Record<string, AtomRow[]>>((acc, a) => {
    (acc[a.kind] ||= []).push(a);
    return acc;
  }, {});

  const memBase = status?.memory.base_url || settings?.atom_memory_base_url || "http://127.0.0.1:8020";
  const space = status?.memory.space_uid || settings?.memory_space_uid || "";

  return (
    <div className="app">
      <nav>
        <div className="brand">
          Velora
          <span>对话 · 长期记忆</span>
        </div>
        <button type="button" className={tab === "chat" ? "active" : ""} onClick={() => setTab("chat")}>
          对话
        </button>
        <button type="button" className={tab === "memory" ? "active" : ""} onClick={() => setTab("memory")}>
          记忆
        </button>
        <button type="button" className={tab === "settings" ? "active" : ""} onClick={() => setTab("settings")}>
          设置
        </button>
        <label className="session-pick">
          会话
          <select
            value={session?.id || ""}
            onChange={(e) => {
              const s = sessions.find((x) => x.id === e.target.value);
              if (s) void selectSession(s);
            }}
          >
            {sessions.map((s) => (
              <option key={s.id} value={s.id}>
                {s.id.slice(0, 8)} · w{s.warmth ?? 35}
              </option>
            ))}
          </select>
        </label>
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
        <div className="nav-actions">
          <button type="button" className="btn ghost" onClick={() => void newSession()}>
            新会话
          </button>
          <button
            type="button"
            className="btn ghost"
            onClick={() => void api.consolidate().then((r) => pushLog("consolidate", r, false))}
          >
            手动固化
          </button>
          <button type="button" className="btn ghost" onClick={() => void probe()}>
            探测状态
          </button>
        </div>
        <div className="status-pills">
          <span className={`pill ${status?.llm.ok ? "ok" : "bad"}`}>llm {status?.llm.ok ? "ok" : "?"}</span>
          <span className={`pill ${status?.memory.ok ? "ok" : "bad"}`}>
            memory {status?.memory.ok ? "ok" : "?"}
          </span>
          <span className="pill">{status?.llm.model || settings?.llm_model || "—"}</span>
          <span className="pill">{session?.id.slice(0, 8) || "—"}</span>
        </div>
        <div className="ext-links">
          <a href={`${memBase.replace(/\/$/, "")}/ui?uid=${encodeURIComponent(space)}`} target="_blank" rel="noreferrer">
            atom /ui
          </a>
          <a href={`${memBase.replace(/\/$/, "")}/chat`} target="_blank" rel="noreferrer">
            atom /chat
          </a>
        </div>
      </nav>
      <main>
        {tab === "chat" && (
          <div className="split">
            <div className="panel">
              <h1>{settings?.persona_name || "日常助理"}</h1>
              <p className="sub">每轮召回 → 组 prompt → 流式回复；右侧 Debug 默认折叠。</p>
              <div className="messages">
                {msgs.map((m, i) => (
                  <div key={i} className={`bubble ${m.role}`}>
                    {m.content || (m.streaming ? "…" : "")}
                  </div>
                ))}
                <div ref={bottomRef} />
              </div>
              {error && <div className="error">{error}</div>}
              <div className="composer">
                <textarea
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  placeholder="说点什么…（Enter 发送；「请记住」立刻固化）"
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
            <div className="panel debug">
              <div className="debug-head">
                <h1>Debug</h1>
                <button type="button" className="btn ghost" onClick={() => setLogs([])}>
                  清空
                </button>
              </div>
              <p className="sub">memory_recall / prompt_ready / persisted</p>
              <div className="log-list">
                {logs.length === 0 && <p className="sub">发送一条消息后这里会出现事件。</p>}
                {logs.map((l) => (
                  <details key={l.id} className="log" open={l.open}>
                    <summary>
                      [{l.t}] {l.title}
                    </summary>
                    <pre>{typeof l.payload === "string" ? l.payload : JSON.stringify(l.payload, null, 2)}</pre>
                  </details>
                ))}
              </div>
            </div>
          </div>
        )}

        {tab === "memory" && (
          <div className="split memory">
            <div className="panel">
              <h1>记忆</h1>
              <p className="sub">atom-memory 原子列表 · 点开看 revisions / evidence</p>
              <div className="row">
                <button type="button" className="btn ghost" onClick={() => void refreshAtoms()}>
                  刷新
                </button>
                <button type="button" className="btn ghost" onClick={() => void api.consolidate().then(refreshAtoms)}>
                  立即固化
                </button>
              </div>
              <div className="form" style={{ marginBottom: "1rem" }}>
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
                      return refreshAtoms();
                    })
                  }
                >
                  写入并固化
                </button>
              </div>
              {error && <div className="error">{error}</div>}
              <div className="atom-list">
                {atoms.length === 0 && <p className="sub">还没有原子记忆。</p>}
                {Object.keys(atomsByKind)
                  .sort()
                  .map((kind) => (
                    <div key={kind}>
                      <div className="kind-label">
                        {kind} ({atomsByKind[kind].length})
                      </div>
                      {atomsByKind[kind].map((a) => (
                        <button
                          type="button"
                          className={`atom-btn ${atomDetail?.key === a.key ? "on" : ""}`}
                          key={a.key}
                          onClick={() => void openAtom(a.key)}
                        >
                          <div>{a.statement}</div>
                          <div className="meta">{a.key}</div>
                        </button>
                      ))}
                    </div>
                  ))}
              </div>
            </div>
            <div className="panel">
              <h1>原子详情</h1>
              {!atomDetail && <p className="sub">选择左侧原子</p>}
              {atomDetail && (
                <>
                  <h2 className="atom-title">{atomDetail.statement}</h2>
                  <div className="row">
                    <span className="pill">{atomDetail.kind}</span>
                    <span className="pill">{atomDetail.key}</span>
                    <span className="pill">{atomDetail.status || "—"}</span>
                    <button
                      type="button"
                      className="btn danger"
                      onClick={() =>
                        void api.archiveAtom(atomDetail.key).then(() => {
                          setAtomDetail(null);
                          return refreshAtoms();
                        })
                      }
                    >
                      归档
                    </button>
                  </div>
                  <p className="sub">detail</p>
                  <pre className="code">{atomDetail.detail || "（空）"}</pre>
                  <p className="sub">revisions</p>
                  <table>
                    <thead>
                      <tr>
                        <th>seq</th>
                        <th>reason</th>
                        <th>trigger</th>
                        <th>statement</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(atomDetail.revisions || []).map((r) => (
                        <tr key={r.seq}>
                          <td>{r.seq}</td>
                          <td>{r.change_reason}</td>
                          <td>{r.trigger}</td>
                          <td>{r.statement}</td>
                        </tr>
                      ))}
                      {(atomDetail.revisions || []).length === 0 && (
                        <tr>
                          <td colSpan={4}>（无）</td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                  <p className="sub">evidence</p>
                  <table>
                    <thead>
                      <tr>
                        <th>rev</th>
                        <th>source</th>
                        <th>kind</th>
                        <th>at</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(atomDetail.evidence || []).map((e, i) => (
                        <tr key={i}>
                          <td>{e.revision_seq}</td>
                          <td>{e.source_id}</td>
                          <td>{e.source_kind}</td>
                          <td>{e.source_occurred_at}</td>
                        </tr>
                      ))}
                      {(atomDetail.evidence || []).length === 0 && (
                        <tr>
                          <td colSpan={4}>（无）</td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </>
              )}
            </div>
          </div>
        )}

        {tab === "settings" && settings && (
          <div className="panel">
            <h1>设置</h1>
            <p className="sub">
              预设 {settings.preset_id} · sidecar {settings.start_memory_sidecar ? "on" : "off"}（环境变量控制拉起）
            </p>
            <form className="form" onSubmit={(e) => void saveSettings(e)}>
              <label>
                LLM Base URL
                <input name="llm_base_url" defaultValue={settings.llm_base_url} key={settings.llm_base_url} />
              </label>
              <label>
                Model
                <input name="llm_model" defaultValue={settings.llm_model} key={settings.llm_model} />
              </label>
              <label>
                API Key {settings.llm_api_key_set ? "（已配置，留空不改）" : ""}
                <input name="llm_api_key" type="password" placeholder="sk-… / EMPTY" autoComplete="off" />
              </label>
              <label style={{ gridTemplateColumns: "auto 1fr", alignItems: "center" }}>
                <input name="tts_enabled" type="checkbox" defaultChecked={settings.tts_enabled} />
                TTS（v0 未接通，仅保存）
              </label>
              <label>
                atom-memory Base URL
                <input
                  name="atom_memory_base_url"
                  defaultValue={settings.atom_memory_base_url}
                  key={settings.atom_memory_base_url}
                />
              </label>
              <label>
                新会话默认 warmth
                <input
                  name="default_warmth"
                  type="number"
                  min={0}
                  max={100}
                  defaultValue={settings.default_warmth}
                  key={settings.default_warmth}
                />
              </label>
              <label>
                memory space
                <input value={settings.memory_space_uid} readOnly />
              </label>
              <button type="submit" className="btn">
                保存
              </button>
            </form>
            {status && (
              <pre className="code status-dump">
                {JSON.stringify(
                  {
                    llm: status.llm,
                    memory: { ok: status.memory.ok, base_url: status.memory.base_url, space_uid: status.memory.space_uid },
                  },
                  null,
                  2,
                )}
              </pre>
            )}
          </div>
        )}
      </main>
    </div>
  );
}
