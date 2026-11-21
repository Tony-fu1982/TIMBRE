export type JobKind = "upload" | "download" | "dna" | "generate" | "convert";
export type JobStatus = "queued" | "running" | "success" | "failed" | "cancelled";
export type RenderState = "" | "submitting" | "pending" | "succeeded" | "failed" | "unknown";

export interface Job {
  id: number;
  kind: JobKind;
  status: JobStatus;
  stage: string;
  progress: string;
  input: string;
  options: Record<string, unknown>;
  result: Record<string, unknown> | null;
  suno_song_id: string;
  render_id: string;
  render_state: RenderState;
  output_path: string;
  error_code: string;
  error_msg: string;
  created_at: string;
  updated_at: string;
  log_seq: number;
}

export const canResumeExport = (j: Job) =>
  j.kind === "upload" && j.render_state === "pending" && !!j.render_id
  && (j.status === "success" || j.status === "failed" || j.status === "cancelled");

export const isResultUncertain = (j: Job) =>
  j.render_state === "unknown" || j.render_state === "submitting"
  || j.error_code === "NETWORK_RESULT_UNKNOWN" || j.error_code === "GENERATION_UNCERTAIN"
  || j.result?.generation_pending === true;

export interface LogLine {
  seq: number;
  message: string;
  created_at: string;
}

export interface SessionState {
  state: "idle" | "login_open" | "waiting_manual" | "verifying" | "logged_in" | "not_logged_in";
  account: string;
}

export interface Settings {
  download_dir: string;
  debug_logs: string;
}

export interface AiConfig {
  provider: "deepseek" | "qwen";
  model: string;
  base_url: string;
  key_set: boolean;
}

export interface JobEvent {
  id: number;
  status: JobStatus;
  stage: string;
  progress: string;
  log_seq: number;
}

export const STATUS_TEXT: Record<JobStatus, string> = {
  queued: "等待中",
  running: "进行中",
  success: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

export const KIND_TEXT: Record<JobKind, string> = {
  upload: "上传还原",
  download: "下载",
  dna: "音乐 DNA",
  generate: "AI 生成",
  convert: "格式转换",
};
