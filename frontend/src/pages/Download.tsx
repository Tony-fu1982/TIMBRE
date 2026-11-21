/** 下载页 —— issue 02/03/08 UI：链接输入（每行一条，批量按序入队）+ 下载任务列表 + 日志。 */
import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Button, EmptyState, Field, LogPanel, Panel, TaskRow } from "../components";
import { useStore } from "../store";
import type { Job } from "../types";

export function DownloadPage() {
  const { session, jobs, login, toast, errText, fetchLogs } = useStore();
  const [text, setText] = useState("");
  const [format, setFormat] = useState("mp3");
  const [mode, setMode] = useState("song");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);

  const list = jobs.filter((j) => j.kind === "download"); // store 已按新→旧排序
  const current = list.find((j) => j.id === selected) ?? list[0] ?? null;
  const currentId = current?.id ?? null;
  useEffect(() => { if (currentId != null) void fetchLogs(currentId); }, [currentId, fetchLogs]);

  const loggedIn = session.state === "logged_in";

  async function submit() {
    const lines = text.split("\n").map((s) => s.trim()).filter(Boolean);
    if (!lines.length) { setError("请输入 Suno 分享链接"); return; }
    setError(""); setSubmitting(true);
    let ok = 0; let last: Job | null = null; let firstErr = "";
    const failed: string[] = [];
    for (let i = 0; i < lines.length; i++) {
      try {
        last = await api.submitDownload(lines[i], format, mode); // 忙时也入队，由后端决定
        ok++;
      } catch (e) {
        failed.push(lines[i]);
        if (!firstErr) firstErr = (lines.length > 1 ? `第 ${i + 1} 条：` : "") + errText(e);
        if (e instanceof ApiError && e.code === "NOT_LOGGED_IN") {
          failed.push(...lines.slice(i + 1)); // 登录失效，其余链接必然同样失败
          break;
        }
      }
    }
    setSubmitting(false);
    if (ok) {
      toast(ok > 1 ? `已加入下载队列（${ok} 条）` : "已加入下载队列", "success");
      if (last) setSelected(last.id);
    }
    setText(failed.join("\n")); // 全部成功则清空；失败链接保留供修改
    if (firstErr) setError(firstErr);
  }

  async function retry(j: Job) {
    try {
      const n = await api.retry(j.id);
      setSelected(n.id);
      toast("已重新加入队列", "success");
    } catch (e) { toast(errText(e), "error"); }
  }

  return (
    <>
    <h1 className="page-title" style={{ marginBottom: "var(--sp-4)" }}>下载</h1>
    <Panel>
        <div className="stack">
          <Field label="分享链接" error={error}>
            <textarea
              className="textarea" rows={3} aria-label="分享链接"
              placeholder="https://suno.com/song/…"
              value={text} onChange={(e) => setText(e.target.value)}
            />
          </Field>
          <div className="row" style={{ alignItems: "flex-start", marginTop: -15 }}>
            <div style={{ flex: 1 }}>
              <Field label="格式">
                <select className="select" aria-label="格式" value={format} onChange={(e) => setFormat(e.target.value)}>
                  <option value="mp3">MP3</option>
                  <option value="wav">WAV</option>
                </select>
              </Field>
            </div>
            <div style={{ flex: 1 }}>
              <Field label="内容" hint="分轨与人声产出 ZIP">
                <select className="select" aria-label="内容" value={mode} onChange={(e) => setMode(e.target.value)}>
                  <option value="song">整首</option>
                  <option value="stems">分轨</option>
                  <option value="vocal">人声</option>
                </select>
              </Field>
            </div>
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: -5 }}>
            <Button style={{ width: 200 }} disabled={submitting} onClick={() => void submit()}>加入下载队列</Button>
          </div>
          {!loggedIn && (
            <div className="row">
              <span className="caption" style={{ color: "var(--danger)" }}>
                {session.state === "waiting_manual" || session.state === "verifying"
                  ? "等待完成 Suno 登录" : "未登录 Suno，无法提交任务"}
              </span>
              <Button sm variant="ghost" onClick={() => void login()}>去登录</Button>
            </div>
          )}
        </div>
    </Panel>

    <div className="two-col" style={{ marginTop: "var(--sp-4)" }}>
      <Panel title="下载任务">
        {list.length === 0 ? <EmptyState text="暂无下载任务" /> : (
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {list.map((j) => (
              <TaskRow key={j.id} job={j} selected={j.id === currentId}
                onSelect={(job) => setSelected(job.id)}
                onRetry={(job) => void retry(job)} />
            ))}
          </div>
        )}
      </Panel>

      <Panel title="日志">
        <LogPanel jobId={currentId} />
      </Panel>
    </div>
    </>
  );
}
