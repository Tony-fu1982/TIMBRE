import type { JobEvent, SessionState } from "./types";

/** SSE 订阅（SPEC §3）：先订阅再拉快照；断线由 EventSource 自动重连，重连后全量刷新+按 seq 去重。 */
export function connectEvents(handlers: {
  onJob: (e: JobEvent) => void;
  onSession: (s: SessionState) => void;
  onOpen?: () => void;
}): () => void {
  const es = new EventSource("/api/events");
  es.addEventListener("job", (ev) => {
    try { handlers.onJob(JSON.parse((ev as MessageEvent).data)); } catch { /* 忽略坏帧 */ }
  });
  es.addEventListener("session", (ev) => {
    try { handlers.onSession(JSON.parse((ev as MessageEvent).data)); } catch { /* 忽略坏帧 */ }
  });
  es.onopen = () => handlers.onOpen?.();
  return () => es.close();
}