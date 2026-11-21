import { useEffect, useRef, useState } from "react";
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Check, Copy, FolderOpen, ListMusic, LogIn, RotateCcw, X } from "lucide-react";
import { useStore } from "../store";
import { canResumeExport, isResultUncertain, KIND_TEXT, STATUS_TEXT } from "../types";
import type { Job, JobStatus } from "../types";

/* ---- 基础控件 ---- */

export function Button({ variant = "primary", sm, className = "", ...rest }:
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "ghost" | "danger"; sm?: boolean }) {
  return <button className={`btn btn-${variant}${sm ? " btn-sm" : ""} ${className}`} {...rest} />;
}

export function Field({ label, error, hint, children }:
  { label: string; error?: string; hint?: string; children: ReactNode }) {
  return (
    <div className="field">
      <span className="field-label">{label}</span>
      {children}
      {error ? <span className="field-error" role="alert">{error}</span>
        : hint ? <span className="field-hint">{hint}</span> : null}
    </div>
  );
}

export function Panel({ title, extra, children }: { title?: string; extra?: ReactNode; children: ReactNode }) {
  return (
    <section className="panel">
      {title && <div className="panel-head"><h2 className="section-title">{title}</h2>{extra}</div>}
      {children}
    </section>
  );
}

export function Badge({ status }: { status: JobStatus }) {
  return <span className={`badge badge-${status}`}>{STATUS_TEXT[status]}</span>;
}

export function ProgressBar({ value }: { value: string }) {
  let pct: number | null = null;
  const frac = value.match(/^(\d+(?:\.\d+)?)\s*\/\s*(\d+(?:\.\d+)?)$/);
  const percent = value.match(/^(\d+(?:\.\d+)?)%$/);
  if (frac && +frac[2] > 0) pct = Math.min(100, (+frac[1] / +frac[2]) * 100);
  else if (percent) pct = Math.min(100, +percent[1]);
  return (
    <div className="progress" role="progressbar" aria-valuenow={pct ?? undefined}>
      <div className={`progress-fill${pct === null ? " indeterminate" : ""}`}
        style={pct === null ? undefined : { width: `${pct}%` }} />
    </div>
  );
}

export function EmptyState({ text }: { text: string }) {
  return <div className="empty">{text}</div>;
}

export function CopyButton({ text, label = "复制", iconOnly }:
  { text: string; label?: string; iconOnly?: boolean }) {
  const [done, setDone] = useState(false);
  const tip = done ? "已复制" : label;
  return (
    <Button variant="ghost" sm title={tip} aria-label={tip} onClick={async () => {
      try { await navigator.clipboard.writeText(text); }
      catch {
        const ta = document.createElement("textarea");
        ta.value = text; document.body.appendChild(ta); ta.select();
        document.execCommand("copy"); ta.remove();
      }
      setDone(true); setTimeout(() => setDone(false), 1500);
    }}>
      {done ? <Check size={14} /> : <Copy size={14} />}{!iconOnly && tip}
    </Button>
  );
}

/* ---- 文件选择 ---- */

export function FileDrop({ accept, file, onFile, hint }:
  { accept: string; file: File | null; onFile: (f: File | null) => void; hint?: string }) {
  const input = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);
  return (
    <div
      className={`filedrop${drag ? " dragover" : ""}${file ? " has-file" : ""}`}
      role="button" tabIndex={0}
      onClick={() => input.current?.click()}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") input.current?.click(); }}
      onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
      onDragLeave={() => setDrag(false)}
      onDrop={(e) => {
        e.preventDefault(); setDrag(false);
        const f = e.dataTransfer.files?.[0];
        if (f) onFile(f);
      }}
    >
      <input ref={input} type="file" accept={accept} hidden
        onChange={(e) => onFile(e.target.files?.[0] ?? null)} />
      {file ? <span>{file.name}（{(file.size / 1048576).toFixed(1)} MB）</span>
        : <span>选择音频{hint ? ` · ${hint}` : ""}（点击或拖入）</span>}
    </div>
  );
}

/* ---- 日志面板：尾随 + 跳到最新 ---- */

export function LogPanel({ jobId }: { jobId: number | null }) {
  const { logs } = useStore();
  const box = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);
  const lines = jobId != null ? logs[jobId] ?? [] : [];

  useEffect(() => {
    if (follow && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines.length, follow, jobId]);

  if (jobId == null) return <div className="logpanel"><EmptyState text="选择一个任务查看日志" /></div>;
  return (
    <div className="logpanel" ref={box}
      onScroll={() => {
        const el = box.current;
        if (el) setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 24);
      }}>
      {lines.length === 0 && <div className="logline muted">暂无日志</div>}
      {lines.map((l) => <div className="logline" key={l.seq}>{l.message}</div>)}
      {!follow && (
        <div className="jump-latest">
          <Button sm variant="ghost" onClick={() => {
            setFollow(true);
            if (box.current) box.current.scrollTop = box.current.scrollHeight;
          }}>跳到最新</Button>
        </div>
      )}
    </div>
  );
}

/* ---- 任务行 ---- */

/** 显示名：自定义备注 > 上传原文件名 > input 原样（URL/主题）> #id。 */
export const jobName = (j: Job): string =>
  String(j.options?.label || j.options?.source_name || j.input || `#${j.id}`);

export function TaskRow({ job, selected, onSelect, onRetry }:
  { job: Job; selected?: boolean; onSelect?: (j: Job) => void; onRetry?: (j: Job) => void }) {
  const { cancel, openFolder, resumeExport } = useStore();
  const [confirming, setConfirming] = useState(false);
  const active = job.status === "queued" || job.status === "running";
  const canResume = canResumeExport(job);
  const resultUrl = job.status === "success" && typeof job.result?.url === "string"
    ? job.result.url : "";
  const sub = job.status === "failed" || job.status === "cancelled"
    ? job.error_msg || job.error_code
    : job.progress || job.stage;
  return (
    <div className={`taskrow${selected ? " selected" : ""}`}
      onClick={() => onSelect?.(job)}>
      <Badge status={job.status} />
      <div className="taskrow-main">
        {onSelect ? (
          <button type="button" className="taskrow-title taskrow-select"
            aria-pressed={!!selected}>{KIND_TEXT[job.kind]} · {jobName(job)}</button>
        ) : <div className="taskrow-title">{KIND_TEXT[job.kind]} · {jobName(job)}</div>}
        {sub && <div className="taskrow-sub">{sub}</div>}
        {resultUrl && (
          <a className="taskrow-sub mono" href={resultUrl} target="_blank"
            rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}>成品链接</a>
        )}
        {job.status === "running" && <ProgressBar value={job.progress} />}
      </div>
      <div className="taskrow-actions" onClick={(e) => e.stopPropagation()}>
        {active && !confirming && (
          <Button sm variant="ghost" title="取消" aria-label="取消" onClick={() => {
            // 只有会丢进度的取消才确认（SPEC：排队取消无请求，直接取消）
            if (job.status === "running") setConfirming(true);
            else void cancel(job.id);
          }}><X size={14} /></Button>
        )}
        {active && confirming && (
          <>
            <Button sm variant="danger" onClick={() => { setConfirming(false); void cancel(job.id); }}>确认取消</Button>
            <Button sm variant="ghost" onClick={() => setConfirming(false)}>继续任务</Button>
          </>
        )}
        {canResume && (
          <Button sm variant="ghost" title="继续查询" aria-label="继续查询" onClick={() => void resumeExport(job.id)}>
            <RotateCcw size={14} />
          </Button>
        )}
        {job.status === "success" && job.output_path && (
          <Button sm variant="ghost" title="打开所在文件夹" aria-label="打开所在文件夹" onClick={() => void openFolder(job.id)}>
            <FolderOpen size={14} />
          </Button>
        )}
        {job.status === "failed" && job.render_state !== "pending" && !isResultUncertain(job) && onRetry && (
          <Button sm variant="ghost" title="重试" aria-label="重试" onClick={() => onRetry(job)}><RotateCcw size={14} /></Button>
        )}
      </div>
    </div>
  );
}

/* ---- 抽屉 / 对话框 ---- */

export function Drawer({ open, title, onClose, children }:
  { open: boolean; title: string; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    ref.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "Tab" && ref.current) {
        const items = ref.current.querySelectorAll<HTMLElement>(
          'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])');
        if (!items.length) return;
        const first = items[0], last = items[items.length - 1];
        if (e.shiftKey && (document.activeElement === first || document.activeElement === ref.current)) {
          e.preventDefault(); last.focus();
        }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("keydown", onKey); prev?.focus(); };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="drawer-mask" onClick={onClose}>
      <div className="drawer" role="dialog" aria-modal="true" aria-label={title}
        ref={ref} tabIndex={-1} onClick={(e) => e.stopPropagation()}>
        <div className="spread" style={{ marginBottom: 16 }}>
          <h2 className="section-title">{title}</h2>
          <Button sm variant="ghost" onClick={onClose} aria-label="关闭"><X size={16} /></Button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function ConfirmDialog({ open, title, body, onConfirm, onCancel, confirmText = "确认" }:
  { open: boolean; title: string; body: string; onConfirm: () => void; onCancel: () => void; confirmText?: string }) {
  if (!open) return null;
  return (
    <div className="dialog-mask" onClick={onCancel}>
      <div className="dialog" role="alertdialog" aria-modal="true" aria-label={title}
        onClick={(e) => e.stopPropagation()}>
        <h2 className="section-title">{title}</h2>
        <p className="muted" style={{ marginTop: 12 }}>{body}</p>
        <div className="dialog-actions">
          <Button variant="ghost" onClick={onCancel}>返回</Button>
          <Button variant="danger" onClick={onConfirm}>{confirmText}</Button>
        </div>
      </div>
    </div>
  );
}

/* ---- 顶栏 ---- */

const TABS = [
  { id: "upload", label: "上传" },
  { id: "download", label: "下载" },
  { id: "dna", label: "音乐 DNA" },
  { id: "convert", label: "格式转换" },
  { id: "settings", label: "设置" },
] as const;
export type TabId = (typeof TABS)[number]["id"];

export function TopBar({ tab, onTab }: { tab: TabId; onTab: (t: TabId) => void }) {
  const { session, login, setQueueOpen, jobs } = useStore();
  const activeCount = jobs.filter((j) => j.status === "queued" || j.status === "running").length;
  const loginUI = session.state === "logged_in"
    ? <span className="login-badge" title={session.account}>
        <span className="dot-ok" aria-hidden="true" />
        {session.account || "Suno"}
      </span>
    : session.state === "waiting_manual" || session.state === "login_open"
      ? <span className="caption">等待手动登录</span>
      : session.state === "verifying"
        ? <span className="caption">正在确认</span>
        : <Button sm variant="danger" onClick={() => void login()}><LogIn size={14} />去登录</Button>;
  return (
    <header className="topbar">
      <span className="brand">TIM<em>BRE</em></span>
      <nav className="tabs" role="tablist" aria-label="主导航">
        {TABS.map((t) => (
          <button key={t.id} role="tab" aria-selected={tab === t.id}
            className="tab" onClick={() => onTab(t.id)}>{t.label}</button>
        ))}
      </nav>
      <div className="topbar-right">
        <Button sm variant="ghost" onClick={() => setQueueOpen(true)}>
          <ListMusic size={14} />队列{activeCount > 0 ? `（${activeCount}）` : ""}
        </Button>
        {loginUI}
      </div>
    </header>
  );
}

export function Toasts() {
  const { toasts } = useStore();
  return (
    <div className="toast-wrap" aria-live="polite">
      {toasts.map((t) => <div key={t.id} className={`toast toast-${t.kind}`}>{t.msg}</div>)}
    </div>
  );
}
