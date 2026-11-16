import { useEffect,  useState } from "react";
import {
  api,
  browserTimezone,



  type AtomRow,
  type DebugStatus,
  type LocalLlmStatus,
  type McpServerEntry,
  type McpStatus,
  type MemorySummary,
  type Persona,


  type Settings,
  type SkillsList,

} from "./api";
import { Operations } from "./Operations";
import { AuthGate } from "./AuthGate";
import { localeOptions, savedLocale, translate, type Locale } from "./i18n";

type McpRow = { name: string; entry: McpServerEntry };

type Tab = "operations" | "memory" | "persona" | "settings";
type LogItem = { id: number; t: string; title: string; payload: unknown; open?: boolean };

let logSeq = 0;

function WorkspaceApp() {
  const [tab, setTab] = useState<Tab>("operations");
  const [locale, setLocale] = useState<Locale>(savedLocale);
  const [error, setError] = useState<string | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [summary, setSummary] = useState<MemorySummary | null>(null);
  const [atomDetail, setAtomDetail] = useState<AtomRow | null>(null);
  const [correction, setCorrection] = useState("");
  const [correctionStatus, setCorrectionStatus] = useState<string | null>(null);
  const [logs, setLogs] = useState<LogItem[]>([]);
  const [status, setStatus] = useState<DebugStatus | null>(null);
  const [tzSuggest, setTzSuggest] = useState("");
  const [mcpStatus, setMcpStatus] = useState<McpStatus | null>(null);
  const [mcpRows, setMcpRows] = useState<McpRow[]>([]);
  const [mcpMsg, setMcpMsg] = useState<string | null>(null);
  const [skillsInfo, setSkillsInfo] = useState<SkillsList | null>(null);
  const [debugOn, setDebugOn] = useState(() => localStorage.getItem("velora_debug") === "1");
  const [localLlm, setLocalLlm] = useState<LocalLlmStatus | null>(null);
  const [localLlmBusy, setLocalLlmBusy] = useState(false);
  const [localLlmMsg, setLocalLlmMsg] = useState<string | null>(null);
  const [personas, setPersonas] = useState<Persona[]>([]);
  const [personaEditId, setPersonaEditId] = useState<string | null>(null);
  const [personaReadonly, setPersonaReadonly] = useState(false);
  const [personaBook, setPersonaBook] = useState<Record<string, unknown> | null>(null);
  const [personaDraft, setPersonaDraft] = useState({
    name: "",
    description: "",
      voice: "vertin",
    personality: "",
    scenario: "",
    opener: "",
    alternate_greetings: "",
    post_history_instructions: "",
    system_prompt: "",
  });
  const [personaMsg, setPersonaMsg] = useState<string | null>(null);

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

  async function loadPersonas() {
    const list = await api.listPersonas();
    setPersonas(list);
    return list;
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
        await probe();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    })();
  }, []);



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

  function startNewPersonaDraft() {
    setPersonaEditId(null);
    setPersonaReadonly(false);
    setPersonaBook(null);
    setPersonaDraft({
      name: "",
      description: "",
      voice: "vertin",
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
      voice: p.voice ?? "vertin",
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
      voice: personaDraft.voice,
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
          <button type="button" className={tab === "operations" ? "active" : ""} onClick={() => setTab("operations")}>
            {locale === "zh-CN" ? "运行后台" : locale === "ja" ? "運用ログ" : "Operations"}
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
        </div>
      </header>

      <main className="stage">
        {tab === "operations" && <Operations locale={locale} />}
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
                      (!personaEditId && p.id === settings?.default_persona_id);
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
                              {p.id === settings?.default_persona_id ? ` · ${t("persona.current")}` : ""}
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
                      disabled={!personaEditId}
                      onClick={() => {
                        if (personaEditId) void api.updateSettings({ default_persona_id: personaEditId }).then(setSettings);
                      }}
                    >
                      {locale === "zh-CN" ? "设为默认人格" : "Set as default"}
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
                      {locale === "zh-CN" ? "声音" : "Voice"} · CosyVoice
                      <select value={personaDraft.voice} onChange={(event) =>
                        setPersonaDraft((draft) => ({ ...draft, voice: event.target.value }))
                      }>
                        <option value="none">{locale === "zh-CN" ? "无语音" : "No voice"}</option>
                        {["Vertin", "Lilya", "Sonetto", "Regulus"].map((voice) =>
                          <option key={voice} value={voice.toLowerCase()}>{voice}</option>
                        )}
                      </select>
                    </label>
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
                    void api.postCorrection(correction.trim()).then((result) => {
                      setCorrection("");
                      setCorrectionStatus(result.status === "consolidated"
                        ? "纠正已生效"
                        : result.status === "queued"
                          ? "纠正已排队，服务恢复后自动补发"
                          : "纠正已保存，等待固化；无需重复提交");
                      return refreshMemory();
                    }).catch((e) => setError(e instanceof Error ? e.message : String(e)))
                  }
                >
                  {t("memory.write")}
                </button>
              </div>
              {correctionStatus && <p role="status">{correctionStatus}</p>}
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

    </div>
  );
}

export default function App() {
  return <AuthGate><WorkspaceApp /></AuthGate>;
}
