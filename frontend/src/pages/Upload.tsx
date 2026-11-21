/** 上传页 —— issue 05 UI：选择音频（≤50MB）→ 阶段进度 + 继续查询 / 结果不明提示 / 重试。 */
import { useEffect, useState } from "react";
import { api } from "../api";
import { Button, CopyButton, EmptyState, Field, FileDrop, jobName, LogPanel, Panel, TaskRow } from "../components";
import { useStore } from "../store";
import type { Job } from "../types";
import { canResumeExport, isResultUncertain } from "../types";

const MAX_BYTES = 50 * 1048576;
const STAGES = ["变形", "上传", "还原", "渲染", "导出"];
// ponytail: 后端 stage 只有 transform/upload/restore/export，渲染与导出同在 export 阶段；
// 若后端细分渲染/导出 stage，在此表加一行即可。
const STAGE_IDX: Record<string, number> = { transform: 0, upload: 1, restore: 2, export: 3 };

function songUrl(j: Job): string {
  const u = j.result?.url;
  if (typeof u === "string" && u) return u;
  return j.suno_song_id ? `https://suno.com/song/${j.suno_song_id}` : "";
}

export function UploadPage() {
  const { session, jobs, login, toast, errText, fetchLogs } = useStore();
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);

  const list = jobs.filter((j) => j.kind === "upload"); // store 已按新→旧排序
  const current = list.find((j) => j.id === selected) ?? list[0] ?? null;
  const currentId = current?.id ?? null;
  useEffect(() => { if (currentId != null) void fetchLogs(currentId); }, [currentId, fetchLogs]);

  const loggedIn = session.state === "logged_in";
  const done = list.filter((j) => j.status === "success" && songUrl(j));

  function pick(f: File | null) {
    if (f && f.size > MAX_BYTES) {
      setFile(null);
      setError("文件超过 50MB，请选择更小的音频");
      return;
    }
    setError(""); setFile(f);
  }

  async function submit() {
    if (!file) return;
    setError(""); setSubmitting(true);
    try {
      const j = await api.submitUpload(file);
      toast("已加入上传队列", "success");
      setFile(null); setSelected(j.id);
    } catch (e) { setError(errText(e)); } // BAD_AUDIO / BUSY / NOT_LOGGED_IN 等中文 message 就地显示
    setSubmitting(false);
  }

  async function retry(j: Job) {
    try {
      const n = await api.retry(j.id);
      setSelected(n.id);
      toast("已重新加入队列", "success");
    } catch (e) { toast(errText(e), "error"); } // 原文件不存在时后端报 BAD_AUDIO，直接展示
  }

  const stageIdx = current && current.status === "running" ? (STAGE_IDX[current.stage] ?? -1) : -1;

  return (
    <>
    <h1 className="page-title" style={{ marginBottom: "var(--sp-4)" }}>上传</h1>
    <div className="two-col">
      <div className="stack">

      <Panel title="上传还原">
        <div className="stack">
          <Field label="音频文件" error={error} hint="不超过 50MB">
            <FileDrop accept="audio/*" file={file} onFile={pick} />
          </Field>
          <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <Button style={{ width: 200 }} disabled={!file || submitting} onClick={() => void submit()}>提交上传</Button>
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

      <Panel title="成品链接" extra={done.length > 1
        ? <CopyButton iconOnly text={done.map(songUrl).join("\n")} label="复制全部" /> : undefined}>
        {done.length === 0 ? <EmptyState text="还没有成功导出的成品" /> : (
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {done.map((j) => {
              const url = songUrl(j);
              return (
                <div className="taskrow" key={j.id}>
                  <div className="taskrow-main">
                    <div className="taskrow-title">{jobName(j)} · #{j.id}</div>
                    <a className="taskrow-sub mono" href={url} target="_blank"
                      rel="noopener noreferrer">{url}</a>
                  </div>
                  <div className="taskrow-actions">
                    <CopyButton iconOnly text={url} label="复制链接" />
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Panel>

      </div>

      <Panel title="上传任务">
        {list.length === 0 ? <EmptyState text="暂无上传任务" /> : (
          <div className="stack">
            {current && (current.status === "queued" || current.status === "running") && (
              <div className="row" style={{ gap: 0, flexWrap: "wrap" }} aria-label="任务阶段">
                {STAGES.map((s, i) => (
                  <span key={s} className="caption" style={{
                    color: i < stageIdx ? "var(--success)" : i === stageIdx ? "var(--accent)" : "var(--text-disabled)",
                    fontWeight: i === stageIdx ? 600 : 500,
                  }}>
                    {i > 0 && <span style={{ color: "var(--text-disabled)", margin: "0 8px" }}>→</span>}
                    {s}
                  </span>
                ))}
              </div>
            )}
            {current && current.render_state === "pending" && (
              <p className="caption" style={{ color: "var(--warning)" }}>
                等待 Suno 渲染{canResumeExport(current) ? "：结果待确认，可点「继续查询」获取同一渲染的结果" : ""}
              </p>
            )}
            {current && (current.status === "failed" || current.status === "cancelled")
              && isResultUncertain(current) && (
              <p className="caption" style={{ color: "var(--danger)" }}>
                请求结果尚未确认，请不要重复提交；先核对 Suno 中的已有结果
                {current.render_id ? "，可用「继续查询」" : ""}
              </p>
            )}
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {current && (
                <TaskRow job={current} selected onRetry={(job) => void retry(job)} />
              )}
            </div>
            <LogPanel jobId={currentId} />
          </div>
        )}
      </Panel>
    </div>
    </>
  );
}
