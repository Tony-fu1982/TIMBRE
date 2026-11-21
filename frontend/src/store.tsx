import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api, ApiError } from "./api";
import { connectEvents } from "./sse";
import type { AiConfig, Job, LogLine, SessionState, Settings } from "./types";

export interface Toast { id: number; msg: string; kind: "info" | "error" | "success" }

interface Store {
  session: SessionState;
  jobs: Job[];
  logs: Record<number, LogLine[]>;
  settings: Settings;
  ai: AiConfig;
  queueOpen: boolean;
  toasts: Toast[];
  /** 最近一次实时 AI_AUTH_FAILED 任务（SPEC §6：跳转 AI 设置）；消费后置 null。 */
  aiAuthFail: number | null;
  clearAiAuthFail: () => void;
  setQueueOpen: (v: boolean) => void;
  toast: (msg: string, kind?: Toast["kind"]) => void;
  refresh: () => Promise<void>;
  fetchLogs: (id: number) => Promise<void>;
  login: () => Promise<void>;
  cancel: (id: number) => Promise<void>;
  openFolder: (id: number) => Promise<void>;
  resumeExport: (id: number) => Promise<void>;
  saveSettings: (patch: Partial<Settings>) => Promise<void>;
  saveAi: (cfg: { provider: string; model: string; api_key?: string; base_url?: string }) => Promise<void>;
  errText: (e: unknown) => string;
}

const Ctx = createContext<Store | null>(null);

export function useStore(): Store {
  const s = useContext(Ctx);
  if (!s) throw new Error("StoreProvider missing");
  return s;
}

export function StoreProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<SessionState>({ state: "not_logged_in", account: "" });
  const [jobs, setJobs] = useState<Job[]>([]);
  const [logs, setLogs] = useState<Record<number, LogLine[]>>({});
  const [settings, setSettings] = useState<Settings>({ download_dir: "", debug_logs: "0" });
  const [ai, setAi] = useState<AiConfig>({ provider: "deepseek", model: "", base_url: "", key_set: false });
  const [queueOpen, setQueueOpen] = useState(false);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [aiAuthFail, setAiAuthFail] = useState<number | null>(null);
  const cursors = useRef<Record<number, number>>({});
  const toastId = useRef(0);

  const toast = useCallback((msg: string, kind: Toast["kind"] = "info") => {
    const id = ++toastId.current;
    setToasts((t) => [...t, { id, msg, kind }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4000);
  }, []);

  const errText = useCallback((e: unknown) =>
    e instanceof ApiError ? e.message : e instanceof Error ? e.message : "未知错误", []);

  const mergeJob = useCallback((j: Job) => {
    setJobs((prev) => {
      const i = prev.findIndex((x) => x.id === j.id);
      if (i < 0) return [j, ...prev];
      const next = [...prev];
      next[i] = j;
      return next;
    });
  }, []);

  /** 按 seq 游标分页拉日志并去重合并（SPEC §3：订阅→快照→日志→去重）。 */
  const fetchLogs = useCallback(async (id: number) => {
    let after = cursors.current[id] ?? 0;
    for (;;) {
      const page = await api.logs(id, after);
      if (page.lines.length) {
        const fresh = page.lines;
        after = fresh[fresh.length - 1].seq;
        cursors.current[id] = after;
        setLogs((prev) => {
          const old = prev[id] ?? [];
          const seen = new Set(old.map((l) => l.seq));
          const add = fresh.filter((l) => !seen.has(l.seq));
          return add.length ? { ...prev, [id]: [...old, ...add] } : prev;
        });
      }
      if (!page.has_more) break;
    }
  }, []);

  const refresh = useCallback(async () => {
    const [j, s, a, sess] = await Promise.all([
      api.jobs(), api.settings(), api.aiConfig(), api.sessionStatus(),
    ]);
    setJobs(j.jobs);
    setSettings(s);
    setAi(a);
    setSession(sess);
  }, []);

  useEffect(() => {
    let alive = true;
    refresh().catch(() => { /* 后端未就绪时静默，SSE 重连后再刷 */ });
    const close = connectEvents({
      onJob: (e) => {
        if (!alive) return;
        api.job(e.id).then((j) => {
          if (!alive) return;
          mergeJob(j);
          // 仅实时失败触发跳转（首次刷新的历史失败不打扰）
          if (j.status === "failed" && j.error_code === "AI_AUTH_FAILED") setAiAuthFail(j.id);
        }).catch(() => {});
        fetchLogs(e.id).catch(() => {});
      },
      onSession: (s) => { if (alive) setSession(s); },
      onOpen: () => { if (alive) refresh().catch(() => {}); },
    });
    return () => { alive = false; close(); };
  }, [refresh, mergeJob, fetchLogs]);

  const login = useCallback(async () => {
    try {
      await api.login();
      toast("已打开 Chrome，请在浏览器里完成 Suno 登录", "info");
    } catch (e) { toast(errText(e), "error"); }
  }, [toast, errText]);

  const cancel = useCallback(async (id: number) => {
    try { mergeJob(await api.cancel(id)); } catch (e) { toast(errText(e), "error"); }
  }, [mergeJob, toast, errText]);

  const openFolder = useCallback(async (id: number) => {
    try { await api.openFolder(id); } catch (e) { toast(errText(e), "error"); }
  }, [toast, errText]);

  const resumeExport = useCallback(async (id: number) => {
    try {
      mergeJob(await api.resumeExport(id));
      toast("已加入继续查询任务", "success");
    } catch (e) { toast(errText(e), "error"); }
  }, [mergeJob, toast, errText]);

  const saveSettings = useCallback(async (patch: Partial<Settings>) => {
    setSettings(await api.saveSettings(patch));  // 失败抛出，由页面显示字段级错误
  }, []);

  const saveAi = useCallback(async (cfg: { provider: string; model: string; api_key?: string; base_url?: string }) => {
    setAi(await api.saveAiConfig(cfg));
  }, []);

  const store: Store = {
    session, jobs, logs, settings, ai, queueOpen, toasts,
    aiAuthFail, clearAiAuthFail: () => setAiAuthFail(null),
    setQueueOpen, toast, refresh, fetchLogs, login, cancel, openFolder,
    resumeExport, saveSettings, saveAi, errText,
  };
  return <Ctx.Provider value={store}>{children}</Ctx.Provider>;
}