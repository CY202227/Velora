import { useEffect, useState } from "react";

type Event = {
  time: string;
  kind: "chat" | "tts";
  model: string;
  status: "ok" | "error";
  tokens: number | null;
  seconds: number;
  tokens_per_second: number | null;
  audio: string | null;
};
type RuntimeRun = { session_id: string; runtime_mode?: string; phase: string; status: string; reason: string | null; step: number; input_tokens: number; output_tokens: number; retries: number; compactions: number };
type RuntimeSnapshot = { active: RuntimeRun[]; recent: RuntimeRun[] };
type Snapshot = { tts_configured: boolean; events: Event[] };

export function Operations({ locale }: { locale: string }) {
  const [snapshot, setSnapshot] = useState<Snapshot>();
  const [runtime, setRuntime] = useState<RuntimeSnapshot>();
  const [offline, setOffline] = useState(false);
  const zh = locale.startsWith("zh");
  const events = snapshot?.events ?? [];
  const speed = (value: number | null | undefined) =>
    value == null ? (zh ? "未采集" : "Unavailable") : value.toFixed(1);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const response = await fetch("/api/debug/inference", { signal: controller.signal });
        if (!response.ok) throw new Error(String(response.status));
        const data: Snapshot = await response.json();
        const runtimeResponse = await fetch("/api/debug/runtime", { signal: controller.signal });
        if (!runtimeResponse.ok) throw new Error(String(runtimeResponse.status));
        const runtimeData: RuntimeSnapshot = await runtimeResponse.json();
        if (active) {
          setSnapshot(data);
          setRuntime(runtimeData);
          setOffline(false);
        }
      } catch {
        if (active) setOffline(true);
      } finally {
        if (active) timer = setTimeout(poll, 3000);
      }
    }
    void poll();
    return () => {
      active = false;
      controller.abort();
      clearTimeout(timer);
    };
  }, []);

  return (
    <section className="operations">
      <h1>{zh ? "运行后台" : "Operations"}</h1>
      <p className="sub">{zh ? "通过 CLI 发起对话，在这里查看模型运行记录。" : "Use the CLI for conversations and inspect model activity here."}</p>
      {offline && <p role="status">{zh ? "日志连接中断，保留最后一次结果。" : "Connection lost. Showing the last available results."}</p>}
      {runtime && (
        <section aria-label={zh ? "模型运行状态" : "Model runtime status"}>
          <h2>{zh ? "模型运行状态" : "Model runtime status"}</h2>
          <p className="sub">{zh ? "只反映模型阶段；完成不代表业务任务已验收。运行记录暂存内存。" : "Model-stage status, not business acceptance. Records are in memory."}</p>
          {[...runtime.active, ...runtime.recent].slice(0, 20).map((run, i) => (
            <p key={`${run.session_id}-${i}`}>
              <code>{run.session_id.slice(0, 12)}</code> · {run.runtime_mode ?? "unknown"} · {run.status} · {run.phase} · {run.step} {zh ? "步" : "steps"}
              {` · ${run.input_tokens}/${run.output_tokens} tokens · ${run.retries} retries · ${run.compactions} compactions`}
              {run.reason && ` · ${run.reason}`}
            </p>
          ))}
        </section>
      )}
      <div className="operations-metrics">
        {(["chat", "tts"] as const).map((kind) => {
          const last = events.find((event) => event.kind === kind);
          return (
            <article key={kind}>
              <h2>{kind === "chat" ? "Chat" : "Vertin TTS"}</h2>
              <strong>{speed(last?.tokens_per_second)}</strong>
              <p>{kind === "chat"
                ? (zh ? "输出 token/s · 请求全程" : "Output tokens/s · full request")
                : (zh ? "语音 token/s · 语音解码" : "Speech tokens/s · speech decoding")}</p>
              {kind === "tts" && snapshot && !snapshot.tts_configured && (
                <p className="sub">{zh ? "Vertin 推理入口尚未配置" : "Vertin inference is not configured"}</p>
              )}
            </article>
          );
        })}
      </div>
      <h2>{zh ? "最近推理日志" : "Recent inference"}</h2>
      {events.length === 0 && <p className="sub">{zh ? "暂无推理记录" : "No inference events yet"}</p>}
      {events.map((event, i) => (
        <article className="operations-event" key={event.time + i}>
          <div><strong>{event.kind === "chat" ? "Chat" : "TTS"} · {event.model}</strong> · {event.status === "ok" ? (zh ? "成功" : "OK") : (zh ? "失败" : "Failed")}</div>
          <div className="sub">{new Date(event.time).toLocaleString(locale)} · {event.tokens ?? "—"} token · {event.seconds.toFixed(2)} s · {speed(event.tokens_per_second)}{event.tokens_per_second != null && " token/s"}</div>
          {event.audio && <audio controls preload="none" src={`/api/debug/speech/${event.audio}`} />}
        </article>
      ))}
    </section>
  );
}
