import type { AiConfig, Job, LogLine, SessionState, Settings } from "./types";

export class ApiError extends Error {
  code: string;
  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, init);
  const body = await r.json().catch(() => null);
  if (!r.ok) {
    const d = body?.detail;
    throw new ApiError(d?.code ?? "INTERNAL_ERROR", d?.message ?? `请求失败（${r.status}）`);
  }
  return body as T;
}

const json = (method: string, data: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(data),
});

function multipart(file: File, extra: Record<string, string>): RequestInit {
  const fd = new FormData();
  fd.append("file", file);
  for (const [k, v] of Object.entries(extra)) fd.append(k, v);
  return { method: "POST", body: fd };
}

export const api = {
  sessionStatus: () => req<SessionState>("/api/session/status"),
  login: () => req<{ state: string }>("/api/session/login", { method: "POST" }),

  jobs: (limit = 50) => req<{ jobs: Job[] }>(`/api/jobs?limit=${limit}`),
  job: (id: number) => req<Job>(`/api/jobs/${id}`),
  logs: (id: number, afterSeq: number, limit = 200) =>
    req<{ lines: LogLine[]; has_more: boolean }>(
      `/api/jobs/${id}/logs?after_seq=${afterSeq}&limit=${limit}`),
  cancel: (id: number) =>
    req<{ ok: boolean }>(`/api/jobs/${id}/cancel`, { method: "POST" }).then(() => api.job(id)),
  openFolder: (id: number) => req<{ ok: boolean }>(`/api/jobs/${id}/open-folder`, { method: "POST" }),
  resumeExport: (id: number) =>
    req<{ job_id: number }>(`/api/jobs/${id}/resume-export`, { method: "POST" })
      .then((r) => api.job(r.job_id)),
  retry: (id: number) => req<Job>(`/api/jobs/${id}/retry`, { method: "POST" }),

  // SPEC §3：提交一律返回 201 {job_id}；这里链一次 GET 拿完整 Job，页面无需感知。
  submitDownload: (url: string, format: string, mode: string) =>
    req<{ job_id: number }>("/api/jobs/download", json("POST", { url, format, mode }))
      .then((r) => api.job(r.job_id)),
  submitUpload: (file: File) =>
    req<{ job_id: number }>("/api/jobs/upload", multipart(file, {})).then((r) => api.job(r.job_id)),
  submitDna: (file: File, mode: string, label?: string) =>
    req<{ job_id: number }>("/api/jobs/dna", multipart(file, { mode, ...(label ? { label } : {}) }))
      .then((r) => api.job(r.job_id)),
  setLabel: (id: number, label: string) =>
    req<Job>(`/api/jobs/${id}/label`, json("POST", { label })),
  submitGenerate: (body: { mode: string; theme?: string; dna_result_id?: number }) =>
    req<{ job_id: number }>("/api/jobs/generate", json("POST", body))
      .then((r) => api.job(r.job_id)),
  submitConvert: (file: File, target: string) =>
    req<{ job_id: number }>("/api/jobs/convert", multipart(file, { target }))
      .then((r) => api.job(r.job_id)),

  settings: () => req<Settings>("/api/settings"),
  saveSettings: (patch: Partial<Settings>) =>
    req<{ ok: boolean }>("/api/settings", json("PUT", patch)).then(() => api.settings()),
  aiConfig: (provider?: AiConfig["provider"]) =>
    req<AiConfig>(provider ? `/api/ai/config?provider=${encodeURIComponent(provider)}` : "/api/ai/config"),
  saveAiConfig: (cfg: { provider: string; model: string; api_key?: string; base_url?: string }) =>
    req<{ ok: boolean }>("/api/ai/config", json("PUT", cfg)).then(() => api.aiConfig()),
};
